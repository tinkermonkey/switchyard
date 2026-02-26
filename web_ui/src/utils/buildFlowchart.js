import { MarkerType } from '@xyflow/react'
import { identifyCycles } from './cycleLayout'

/**
 * Builds React Flow nodes and edges from pipeline run events.
 * Returns unpositioned nodes so the caller can apply applyCycleLayout() with their own options.
 *
 * @param {Object} params
 * @param {Array}  params.events              - Pipeline run events (all events)
 * @param {Map}    params.existingCycles       - Existing cycles map to preserve collapse state
 * @param {Object} params.workflowConfig       - Workflow configuration for cycle detection
 * @param {Object} params.selectedPipelineRun  - Pipeline run metadata
 * @param {Set}    params.activeAgentNames     - Set of currently-active agent names
 * @returns {{ nodes, edges, agentExecutions, updatedCycles }}
 */
export function buildFlowchart({
  events,
  existingCycles = new Map(),
  workflowConfig = null,
  selectedPipelineRun,
  activeAgentNames = new Set(),
}) {
  if (!events.length || !selectedPipelineRun) {
    return { nodes: [], edges: [], agentExecutions: new Map(), updatedCycles: new Map() }
  }

  const newNodes = []
  const newEdges = []

  // Add pipeline created node
  newNodes.push({
    id: 'created',
    type: 'pipelineEvent',
    position: { x: 0, y: 0 },
    data: {
      label: 'Pipeline Started',
      type: 'pipeline_created',
      metadata: new Date(selectedPipelineRun.started_at).toLocaleString(),
    },
    draggable: false,
  })

  // Track agent executions: map of agent -> [execution instances]
  const agentExecutions = new Map()

  // First pass: identify all agent executions and their status
  events.forEach(event => {
    if (event.event_category === 'agent_lifecycle') {
      const agent = event.agent
      const taskId = event.task_id

      if (event.event_type === 'agent_initialized') {
        if (!agentExecutions.has(agent)) {
          agentExecutions.set(agent, [])
        }
        agentExecutions.get(agent).push({
          taskId,
          startTime: event.timestamp,
          startEvent: event,
          endTime: null,
          endEvent: null,
          status: 'running',
          isActive: activeAgentNames.has(agent),
        })
      } else if (['agent_completed', 'agent_failed'].includes(event.event_type)) {
        const executions = agentExecutions.get(agent) || []
        const execution = executions.find(e => e.taskId === taskId)
        if (execution) {
          execution.endTime = event.timestamp
          execution.endEvent = event
          execution.status = event.event_type === 'agent_completed' ? 'completed' : 'failed'
        }
      }
    }
  })

  // Identify review/repair cycles
  const detectedCycles = identifyCycles(events, agentExecutions, workflowConfig)

  // Merge with existing cycle state (preserve collapse state)
  const updatedCycles = new Map(detectedCycles)
  existingCycles.forEach((existingCycle, cycleKey) => {
    if (updatedCycles.has(cycleKey)) {
      updatedCycles.set(cycleKey, {
        ...updatedCycles.get(cycleKey),
        isCollapsed: existingCycle.isCollapsed,
      })
    }
  })

  // Second pass: build nodes and edges chronologically
  let previousNodeId = 'created'

  const sortedEvents = [...events].sort((a, b) =>
    new Date(a.timestamp) - new Date(b.timestamp)
  )

  sortedEvents.forEach((event, idx) => {
    let currentNodeId = null

    // Decision events
    if (event.event_category === 'decision') {
      const nodeId = `decision-${idx}`
      const decisionType = event.event_type || 'decision'
      const reason = event.reason || ''

      const metadataParts = []
      if (event.decision_category) {
        metadataParts.push(`[${event.decision_category}]`)
      }
      if (event.decision) {
        if (event.decision.selected_agent) metadataParts.push(`→ ${event.decision.selected_agent}`)
        if (event.decision.to_status) metadataParts.push(`→ ${event.decision.to_status}`)
        if (event.decision.action) metadataParts.push(`${event.decision.action}`)
      }
      if (reason) {
        const maxLen = 50
        const truncated = reason.length > maxLen ? reason.substring(0, maxLen) + '...' : reason
        metadataParts.push(truncated)
      }

      newNodes.push({
        id: nodeId,
        type: 'pipelineEvent',
        position: { x: 0, y: 0 },
        data: {
          label: decisionType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
          type: 'decision_event',
          metadata: metadataParts.join(' • '),
          decision_category: event.decision_category,
          timestamp: event.timestamp,
        },
        draggable: false,
      })
      currentNodeId = nodeId
    }

    // Agent execution starts
    else if (event.event_category === 'agent_lifecycle' && event.event_type === 'agent_initialized') {
      const agent = event.agent
      const taskId = event.task_id
      const executions = agentExecutions.get(agent) || []
      const executionIndex = executions.findIndex(e => e.taskId === taskId)

      // Check for legacy simple-cycle (cycle keyed by agent name with executions array)
      if (updatedCycles.has(agent)) {
        const cycleExecutions = updatedCycles.get(agent).executions
        const cycleIndex = cycleExecutions.findIndex(e => e.taskId === taskId)
        const nodeId = `agent-${agent}-${cycleIndex}`
        const execution = cycleExecutions[cycleIndex]
        const isActive = execution.isActive

        newNodes.push({
          id: nodeId,
          type: 'pipelineEvent',
          position: { x: 0, y: 0 },
          data: {
            label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
            type: 'agent_execution',
            status: execution.status,
            metadata: `Iteration ${cycleIndex + 1}${isActive ? ' (Running)' : ''}`,
            isActive,
          },
          draggable: false,
        })

        if (cycleIndex > 0) {
          const previousExecutionId = `agent-${agent}-${cycleIndex - 1}`
          newEdges.push({
            id: `feedback-${agent}-${cycleIndex}`,
            source: previousExecutionId,
            target: nodeId,
            type: 'smoothstep',
            label: 'Revision',
            labelStyle: { fontSize: '10px', fill: '#f59e0b' },
            markerEnd: { type: MarkerType.ArrowClosed, color: '#f59e0b' },
            style: { stroke: '#f59e0b', strokeDasharray: '5,5' },
          })
        }

        currentNodeId = nodeId
      } else {
        // Single execution agent (or agent in a boundary-detected cycle)
        const nodeId = `agent-${agent}-${executionIndex}`
        const execution = executions[executionIndex]
        const isActive = execution.isActive

        newNodes.push({
          id: nodeId,
          type: 'pipelineEvent',
          position: { x: 0, y: 0 },
          data: {
            label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
            type: 'agent_execution',
            status: execution.status,
            metadata: isActive ? 'Running' : execution.status,
            isActive,
          },
          draggable: false,
        })

        currentNodeId = nodeId
      }
    }

    // Connect to previous node
    if (currentNodeId && previousNodeId) {
      newEdges.push({
        id: `edge-${previousNodeId}-${currentNodeId}`,
        source: previousNodeId,
        target: currentNodeId,
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed, color: '#6e7681' },
        style: { stroke: '#6e7681' },
      })
      previousNodeId = currentNodeId
    }
  })

  // Add pipeline completed node if pipeline is done
  if (selectedPipelineRun.status === 'completed') {
    newNodes.push({
      id: 'completed',
      type: 'pipelineEvent',
      position: { x: 0, y: 0 },
      data: {
        label: 'Pipeline Completed',
        type: 'pipeline_completed',
        metadata: selectedPipelineRun.ended_at
          ? new Date(selectedPipelineRun.ended_at).toLocaleString()
          : '',
      },
      draggable: false,
    })

    if (previousNodeId !== 'created') {
      newEdges.push({
        id: `edge-${previousNodeId}-completed`,
        source: previousNodeId,
        target: 'completed',
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed, color: '#6e7681' },
        style: { stroke: '#6e7681' },
      })
    }
  }

  return { nodes: newNodes, edges: newEdges, agentExecutions, updatedCycles }
}
