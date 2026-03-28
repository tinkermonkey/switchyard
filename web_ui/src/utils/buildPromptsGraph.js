import { MarkerType } from '@xyflow/react'

// Layout constants
export const PROMPT_NODE_WIDTH = 800
export const HALF_GAP = 10
export const AGENT_SPACING = 1700       // center-to-center
export const START_NODE_WIDTH = 200
export const START_NODE_HEIGHT = 80
export const AGENT_NODE_WIDTH = 250
export const AGENT_NODE_HEIGHT = 80
export const AGENT_ROW_Y = 50
export const PROMPT_ROW_Y = 210         // AGENT_ROW_Y + AGENT_NODE_HEIGHT + 80
export const FIRST_AGENT_CENTER_X = 1060 // START_NODE_WIDTH + 50 + PROMPT_NODE_WIDTH + HALF_GAP

function formatAgentLabel(agentName) {
  return agentName.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
}

function formatDuration(ms) {
  if (ms == null || ms < 0) return null
  const totalSec = Math.round(ms / 1000)
  if (totalSec < 60) return `${totalSec}s`
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return s > 0 ? `${m}m ${s}s` : `${m}m`
}

function formatDurationSeconds(sec) {
  if (sec == null || sec < 0) return null
  return formatDuration(sec * 1000)
}

function extractMdFiles(promptText) {
  if (!promptText) return []
  const regex = /`([^`]+\.md)`/g
  const files = []
  let match
  while ((match = regex.exec(promptText)) !== null) {
    if (!files.includes(match[1])) files.push(match[1])
  }
  return files
}

/**
 * Builds ReactFlow nodes and edges for the Prompts timeline graph.
 *
 * @param {Object} params
 * @param {Array}  params.events              - mergedEvents (all pipeline run events)
 * @param {Object} params.selectedPipelineRun - Pipeline run metadata
 * @param {Object} params.filters             - { agentName: string|null, showInput: bool, showOutput: bool }
 * @returns {{ nodes: Array, edges: Array }}
 */
export function buildPromptsGraph({ events, selectedPipelineRun, filters = {} }) {
  const { agentName: filterAgent = null, showInput = true, showOutput = true } = filters

  if (!events?.length || !selectedPipelineRun) {
    return { nodes: [], edges: [] }
  }

  // Single-pass: build lookup maps keyed by task_id
  const agentInitMap = new Map()      // task_id → agent_initialized event (normalized)
  const promptMap    = new Map()      // task_id → prompt_constructed event (normalized)
  const completedMap = new Map()      // task_id → agent_completed event (normalized)
  const failedMap    = new Map()      // task_id → agent_failed event (normalized)

  const commentEvents = []            // github_comment_posted events (normalized)
  let pipelineCompletedEvent = null   // first pipeline_run_completed event

  for (const event of events) {
    const taskId = event.task_id

    switch (event.event_type) {
      case 'agent_initialized':
        if (taskId) agentInitMap.set(taskId, {
          ...event,
          agent_execution_id: event.agent_execution_id ?? event.data?.agent_execution_id,
        })
        break
      case 'prompt_constructed':
        if (taskId) promptMap.set(taskId, {
          ...event,
          prompt: event.prompt ?? event.data?.prompt ?? '',
          prompt_components: event.prompt_components ?? event.data?.prompt_components ?? null,
        })
        break
      case 'agent_completed':
        if (taskId) completedMap.set(taskId, {
          ...event,
          output: event.output ?? event.data?.output ?? null,
          duration_ms: event.duration_ms ?? event.data?.duration_ms ?? null,
        })
        break
      case 'agent_failed':
        if (taskId) failedMap.set(taskId, {
          ...event,
          error: event.error ?? event.data?.error ?? null,
        })
        break
      case 'github_comment_posted':
        commentEvents.push({
          ...event,
          body:          event.data?.body          ?? event.body          ?? '',
          title:         event.data?.title         ?? event.title         ?? '',
          object_type:   event.data?.object_type   ?? event.object_type   ?? '',
          object_number: event.data?.object_number ?? event.object_number ?? 0,
        })
        break
      case 'pipeline_run_completed':
        if (!pipelineCompletedEvent) {
          pipelineCompletedEvent = {
            ...event,
            duration_seconds: event.data?.duration_seconds ?? event.duration_seconds ?? null,
            reason:           event.data?.reason           ?? event.reason           ?? 'completed',
          }
        }
        break
    }
  }

  // Inner-join: only agents that also have a prompt_constructed event
  let agentEntries = []
  for (const [taskId, initEvent] of agentInitMap) {
    if (!promptMap.has(taskId)) continue
    agentEntries.push({ kind: 'agent', taskId, initEvent, timestamp: new Date(initEvent.timestamp).getTime() })
  }

  // Sort ascending by start time
  agentEntries.sort((a, b) => a.timestamp - b.timestamp)

  // Apply agent name filter (only to agent entries; comments always shown)
  if (filterAgent) {
    agentEntries = agentEntries.filter(e => e.initEvent.agent === filterAgent)
  }

  // Normalise comment timestamps for sorting
  const normalizedComments = commentEvents.map(e => ({
    kind: 'comment',
    event: e,
    timestamp: new Date(e.timestamp).getTime(),
  }))

  // Build combined backbone timeline: agents + comments, sorted by timestamp
  const timeline = [...agentEntries, ...normalizedComments].sort((a, b) => a.timestamp - b.timestamp)

  if (timeline.length === 0 && !pipelineCompletedEvent) {
    return { nodes: [], edges: [] }
  }

  const nodes = []
  const edges = []

  // ── Start node ────────────────────────────────────────────────────────────
  const startedAt = selectedPipelineRun.started_at
  const startedLabel = startedAt
    ? new Date(startedAt).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'medium' })
    : ''

  nodes.push({
    id: 'prompts-start',
    type: 'pipelineStarted',
    position: { x: 0, y: AGENT_ROW_Y },
    data: {
      label: 'Pipeline Started',
      metadata: startedLabel,
      event: null,
      isActive: false,
    },
    draggable: false,
    style: { minWidth: START_NODE_WIDTH },
  })

  // ── Timeline entries (agents + comments) ─────────────────────────────────
  let prevBackboneId = 'prompts-start'

  timeline.forEach((entry, idx) => {
    const spineX = FIRST_AGENT_CENTER_X + idx * AGENT_SPACING

    if (entry.kind === 'agent') {
      const { taskId, initEvent } = entry
      const agentName      = initEvent.agent ?? 'unknown'
      const promptEvent    = promptMap.get(taskId)
      const completedEvent = completedMap.get(taskId)
      const failedEvent    = failedMap.get(taskId)

      const status = completedEvent ? 'completed'
                   : failedEvent    ? 'failed'
                   : 'running'
      const isActive   = status === 'running'
      const durationMs = completedEvent?.duration_ms ?? null

      const agentNodeId  = `prompts-agent-${taskId}`
      const inputNodeId  = `prompts-input-${taskId}`
      const outputNodeId = `prompts-output-${taskId}`

      // Agent execution node
      nodes.push({
        id: agentNodeId,
        type: 'agentExecution',
        position: { x: spineX - AGENT_NODE_WIDTH / 2, y: AGENT_ROW_Y },
        data: {
          label: formatAgentLabel(agentName),
          status,
          isActive,
          durationMs,
          inputTokens: null,
          outputTokens: null,
          tools: null,
          event: initEvent,
        },
        draggable: false,
      })

      // Input prompt node
      if (showInput) {
        const promptText       = promptEvent.prompt
        const promptComponents = promptEvent.prompt_components
        const mdFiles          = extractMdFiles(promptText)

        nodes.push({
          id: inputNodeId,
          type: 'promptInput',
          position: { x: spineX - PROMPT_NODE_WIDTH - HALF_GAP, y: PROMPT_ROW_Y },
          data: {
            taskId,
            agentName,
            promptText,
            promptComponents,
            mdFiles,
            timestamp: promptEvent.timestamp,
          },
          draggable: false,
        })

        edges.push({
          id: `edge-agent-input-${taskId}`,
          source: agentNodeId,
          target: inputNodeId,
          type: 'default',
          style: { stroke: '#4b5563', strokeWidth: 1, strokeDasharray: '4 3', opacity: 0.6 },
        })
      }

      // Output node
      if (showOutput) {
        const outputText = completedEvent?.output ?? null
        const errorText  = failedEvent?.error ?? null

        nodes.push({
          id: outputNodeId,
          type: 'promptOutput',
          position: { x: spineX + HALF_GAP, y: PROMPT_ROW_Y },
          data: {
            taskId,
            agentName,
            outputText,
            errorText,
            status,
            durationMs,
            durationStr: formatDuration(durationMs),
            timestamp: (completedEvent ?? failedEvent)?.timestamp ?? null,
          },
          draggable: false,
        })

        edges.push({
          id: `edge-agent-output-${taskId}`,
          source: agentNodeId,
          target: outputNodeId,
          type: 'default',
          style: { stroke: '#4b5563', strokeWidth: 1, strokeDasharray: '4 3', opacity: 0.6 },
        })
      }

      // Backbone edge: prevNode → this agent (left/right handles)
      edges.push({
        id: `edge-backbone-${idx}`,
        source: prevBackboneId,
        sourceHandle: 'right',
        target: agentNodeId,
        targetHandle: 'left',
        type: 'default',
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: '#30363d', strokeWidth: 1.5 },
      })
      prevBackboneId = agentNodeId

    } else {
      // GitHub comment entry
      const { event: commentEvent } = entry
      const commentId    = `prompts-comment-${idx}`
      const commentBodyId = `prompts-comment-body-${idx}`

      const objectLabel  = commentEvent.object_type && commentEvent.object_number
        ? `${commentEvent.object_type} #${commentEvent.object_number}`
        : 'GitHub'
      const commentLabel = commentEvent.title || objectLabel

      const timestampStr = commentEvent.timestamp
        ? new Date(commentEvent.timestamp).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : null

      // Comment backbone node (compact)
      nodes.push({
        id: commentId,
        type: 'pipelineEvent',
        position: { x: spineX - AGENT_NODE_WIDTH / 2, y: AGENT_ROW_Y },
        data: {
          label: commentLabel,
          metadata: timestampStr ? `Posted ${timestampStr}` : objectLabel,
          event: commentEvent,
          isActive: false,
        },
        draggable: false,
        style: { minWidth: AGENT_NODE_WIDTH },
      })

      // Comment body node (full content, centered below backbone)
      if (showOutput) {
        nodes.push({
          id: commentBodyId,
          type: 'promptOutput',
          position: { x: spineX - PROMPT_NODE_WIDTH / 2, y: PROMPT_ROW_Y },
          data: {
            taskId: null,
            agentName: objectLabel,
            outputText: commentEvent.body || null,
            errorText: null,
            status: 'completed',
            durationMs: null,
            durationStr: null,
            timestamp: commentEvent.timestamp ?? null,
          },
          draggable: false,
        })

        edges.push({
          id: `edge-comment-body-${idx}`,
          source: commentId,
          target: commentBodyId,
          type: 'default',
          style: { stroke: '#4b5563', strokeWidth: 1, strokeDasharray: '4 3', opacity: 0.6 },
        })
      }

      // Backbone edge: prevNode → this comment (left/right handles)
      edges.push({
        id: `edge-backbone-${idx}`,
        source: prevBackboneId,
        sourceHandle: 'right',
        target: commentId,
        targetHandle: 'left',
        type: 'default',
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: '#30363d', strokeWidth: 1.5 },
      })
      prevBackboneId = commentId
    }
  })

  // ── Pipeline completed terminal node ──────────────────────────────────────
  if (pipelineCompletedEvent) {
    const completedX = FIRST_AGENT_CENTER_X + timeline.length * AGENT_SPACING
    const durationStr = formatDurationSeconds(pipelineCompletedEvent.duration_seconds)

    nodes.push({
      id: 'prompts-pipeline-completed',
      type: 'pipelineCompleted',
      position: { x: completedX - START_NODE_WIDTH / 2, y: AGENT_ROW_Y },
      data: {
        label: 'Pipeline Completed',
        metadata: durationStr ? `Duration: ${durationStr}` : (pipelineCompletedEvent.reason ?? 'completed'),
        event: pipelineCompletedEvent,
        isActive: false,
      },
      draggable: false,
      style: { minWidth: START_NODE_WIDTH },
    })

    edges.push({
      id: 'edge-backbone-completed',
      source: prevBackboneId,
      sourceHandle: 'right',
      target: 'prompts-pipeline-completed',
      targetHandle: 'left',
      type: 'default',
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { stroke: '#30363d', strokeWidth: 1.5 },
    })
  }

  return { nodes, edges }
}
