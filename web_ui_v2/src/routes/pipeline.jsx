import { createFileRoute } from '@tanstack/react-router'
import { Link } from '@tanstack/react-router'
import { Activity, GitBranch, RefreshCw } from 'lucide-react'
import Header from '../components/Header'
import { useState, useEffect, useCallback, useRef } from 'react'
import {
  ReactFlow,
  MiniMap,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  MarkerType,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useSocket } from '../contexts/SocketContext'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

function PipelineView() {
  const [pipelineData, setPipelineData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [expandedAgent, setExpandedAgent] = useState(null)
  const [selectedExecution, setSelectedExecution] = useState(null)
  const [executionLogs, setExecutionLogs] = useState([])
  const [executionNodes, setExecutionNodes] = useState([])
  const { events } = useSocket()
  const logsContainerRef = useRef(null)

  const formatToolUse = (item) => {
    const toolName = item.name
    const input = item.input || {}

    switch (toolName) {
      case 'Bash':
        return `Bash: ${input.command || ''}`
      case 'Read':
        return `Read: ${input.file_path || ''}`
      case 'Grep':
        return `Grep: "${input.pattern || ''}" in ${input.path || '.'}`
      case 'Edit':
        return `Edit: ${input.file_path || ''}`
      case 'Write':
        return `Write: ${input.file_path || ''}`
      case 'Glob':
        return `Glob: ${input.pattern || ''}`
      default:
        return `${toolName}${input.description ? ': ' + input.description : ''}`
    }
  }

  const getLogContent = (log) => {
    let logType = 'unknown'
    let logContent = ''

    // Handle execution log structure from Redis
    if (log.event) {
      const evt = log.event

      if (evt.type === 'result' && evt.result) {
        logType = evt.subtype === 'success' ? 'text' : 'error'
        logContent = evt.result
      } else if (evt.type === 'tool_use' && evt.tool_name) {
        logType = 'tool'
        logContent = `${evt.tool_name}${evt.description ? ': ' + evt.description : ''}`
      } else if (evt.type === 'error') {
        logType = 'error'
        logContent = evt.error || evt.message || 'Unknown error'
      }
    }
    // Fallback to LiveLogs structure
    else if (log.type === 'assistant') {
      const msg = log.message
      if (msg?.content) {
        const contents = Array.isArray(msg.content) ? msg.content : [msg.content]
        for (const item of contents) {
          if (item.type === 'text') {
            logType = 'text'
            logContent = item.text || ''
            break
          } else if (item.type === 'tool_use') {
            logType = 'tool'
            logContent = formatToolUse(item)
            break
          }
        }
      }
    } else if (log.type === 'user') {
      const msg = log.message
      if (msg?.content) {
        const contents = Array.isArray(msg.content) ? msg.content : [msg.content]
        for (const item of contents) {
          if (item.type === 'tool_result') {
            logType = 'result'
            const preview = item.content?.substring(0, 60) || ''
            logContent = `Tool result${item.is_error ? ' (error)' : ''}: ${preview}${item.content?.length > 60 ? '...' : ''}`
            break
          }
        }
      }
    }

    return { logType, logContent }
  }

  const getLogTypeColor = (type) => {
    switch (type) {
      case 'text': return 'bg-blue-600'
      case 'tool': return 'bg-purple-600'
      case 'result': return 'bg-green-600'
      case 'error': return 'bg-red-600'
      default: return 'bg-gh-border'
    }
  }

  const fetchPipeline = async () => {
    try {
      setLoading(true)
      const response = await fetch('/current-pipeline')
      const data = await response.json()
      console.log('Fetched pipeline data:', data)
      setPipelineData(data)

      // Build ReactFlow nodes and edges from pipeline stages with review cycles
      if (data?.pipeline?.stages) {
        const stageNodes = []
        const stageEdges = []
        let yPosition = 0
        let nodeIdCounter = 0

        data.pipeline.stages.forEach((stage, index) => {
          const isRunning = stage.status === 'running'
          const isCompleted = stage.status === 'completed'
          const isPending = stage.status === 'pending'

          // Main stage node (maker)
          const mainNodeId = String(nodeIdCounter++)
          stageNodes.push({
            id: mainNodeId,
            type: 'default',
            position: { x: 250, y: yPosition },
            sourcePosition: 'right', // Connections go out to the right (for executions)
            targetPosition: 'top', // Connections come in from top (for pipeline flow)
            data: {
              label: (
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontWeight: 'bold', fontSize: '14px', color: '#fff' }}>{String(stage.name)}</div>
                  <div style={{ fontSize: '12px', color: '#d1d5db' }}>{String(stage.agent)}</div>
                  <div style={{ fontSize: '11px', marginTop: '4px', color: '#fff' }}>
                    {isRunning ? '⚡ Running' : isCompleted ? '✓ Completed' : '⏳ Pending'}
                  </div>
                </div>
              ),
              agentName: stage.agent // Store agent name for click handler
            },
            style: {
              background: isRunning ? '#1f6feb' : isCompleted ? '#238636' : '#30363d',
              color: '#fff',
              border: isRunning ? '3px solid #58a6ff' : isCompleted ? '2px solid #2ea043' : '1px solid #6e7681',
              borderRadius: '8px',
              padding: '12px',
              opacity: isPending ? 0.6 : 1,
              boxShadow: isRunning ? '0 0 10px rgba(88, 166, 255, 0.5)' : 'none',
              width: 200,
              cursor: 'pointer',
            }
          })

          yPosition += 150

          // If stage has review, add reviewer node and feedback loop
          if (stage.reviewer_agent) {
            const reviewerNodeId = String(nodeIdCounter++)

            stageNodes.push({
              id: reviewerNodeId,
              type: 'default',
              position: { x: 550, y: yPosition - 75 },
              data: {
                label: (
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontWeight: 'bold', fontSize: '13px', color: '#fff' }}>Review</div>
                    <div style={{ fontSize: '11px', color: '#e9d5ff' }}>{String(stage.reviewer_agent)}</div>
                  </div>
                ),
                agentName: stage.reviewer_agent // Store reviewer agent name for click handler
              },
              style: {
                background: '#7c3aed',
                color: '#fff',
                border: '2px solid #a78bfa',
                borderRadius: '8px',
                padding: '10px',
                opacity: isPending ? 0.5 : 0.9,
                width: 160,
                cursor: 'pointer',
              }
            })

            // Edge from maker to reviewer
            stageEdges.push({
              id: `e${mainNodeId}-${reviewerNodeId}`,
              source: mainNodeId,
              target: reviewerNodeId,
              type: 'smoothstep',
              label: 'Submit',
              labelStyle: { fontSize: '10px', fill: '#888' },
              markerEnd: {
                type: MarkerType.ArrowClosed,
                color: '#a78bfa'
              },
              style: { stroke: '#a78bfa' }
            })

            // Feedback edge from reviewer back to maker
            stageEdges.push({
              id: `e${reviewerNodeId}-${mainNodeId}-feedback`,
              source: reviewerNodeId,
              target: mainNodeId,
              type: 'smoothstep',
              label: 'Revise',
              labelStyle: { fontSize: '10px', fill: '#f59e0b' },
              markerEnd: {
                type: MarkerType.ArrowClosed,
                color: '#f59e0b'
              },
              style: { stroke: '#f59e0b', strokeDasharray: '5,5' }
            })
          }

          // Edge to next stage (from reviewer if exists, otherwise from maker)
          if (index < data.pipeline.stages.length - 1) {
            const sourceNodeId = stage.reviewer_agent ? String(nodeIdCounter - 1) : mainNodeId
            const targetNodeId = String(nodeIdCounter)

            stageEdges.push({
              id: `e${sourceNodeId}-${targetNodeId}`,
              source: sourceNodeId,
              target: targetNodeId,
              type: 'smoothstep',
              label: 'Approve',
              labelStyle: { fontSize: '10px', fill: '#22c55e' },
              animated: data.pipeline.stages[index + 1]?.status === 'running',
              markerEnd: {
                type: MarkerType.ArrowClosed,
                color: '#6e7681'
              },
              style: { stroke: '#6e7681' }
            })
          }
        })

        // Update nodes using proper pattern to avoid reset
        setNodes((prevNodes) => {
          // Keep execution nodes if they exist
          const execNodes = prevNodes.filter(n => n.data?.isExecution)
          return [...stageNodes, ...execNodes]
        })
        setEdges(stageEdges)
      }
    } catch (error) {
      console.error('Error fetching pipeline:', error)
    } finally {
      setLoading(false)
    }
  }

  // Combine pipeline nodes with execution nodes
  const allNodes = [...nodes, ...executionNodes]

  // Add edges from agent nodes to execution nodes (right to left)
  const executionEdges = executionNodes.map(execNode => {
    const agentNode = nodes.find(n => n.data.agentName === expandedAgent)
    return {
      id: `e-${agentNode?.id}-${execNode.id}`,
      source: String(agentNode?.id),
      target: execNode.id,
      type: 'smoothstep',
      animated: false,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: '#6b7280'
      },
      style: { stroke: '#6b7280', strokeWidth: 2 }
    }
  })

  const allEdges = [...edges, ...executionEdges]

  useEffect(() => {
    fetchPipeline()
    // No polling - we'll use WebSocket for updates
  }, [])

  // Listen for WebSocket events to update pipeline in real-time
  useEffect(() => {
    if (events.length === 0) return

    const latestEvent = events[events.length - 1]

    // Update pipeline on relevant events
    if (['agent_initialized', 'agent_completed', 'agent_failed', 'task_received'].includes(latestEvent.event_type)) {
      // Update nodes status without resetting the graph
      setNodes((currentNodes) =>
        currentNodes.map((node) => {
          if (node.data?.agentName === latestEvent.agent) {
            // Determine new status
            let status = node.data?.status || 'pending'
            if (latestEvent.event_type === 'agent_initialized' || latestEvent.event_type === 'task_received') {
              status = 'running'
            } else if (latestEvent.event_type === 'agent_completed') {
              status = 'completed'
            } else if (latestEvent.event_type === 'agent_failed') {
              status = 'failed'
            }

            const isRunning = status === 'running'
            const isCompleted = status === 'completed'
            const isPending = status === 'pending'

            return {
              ...node,
              data: {
                ...node.data,
                status
              },
              style: {
                ...node.style,
                background: isRunning ? '#1f6feb' : isCompleted ? '#238636' : '#30363d',
                border: isRunning ? '3px solid #58a6ff' : isCompleted ? '2px solid #2ea043' : '1px solid #6e7681',
                opacity: isPending ? 0.6 : 1,
                boxShadow: isRunning ? '0 0 10px rgba(88, 166, 255, 0.5)' : 'none',
              }
            }
          }
          return node
        })
      )
    }
  }, [events])

  const onNodeClick = useCallback(async (event, node) => {
    const agentName = node.data.agentName
    const isExecutionNode = node.data.isExecution

    // If clicking an execution node, show logs in side panel
    if (isExecutionNode) {
      const execution = node.data.execution
      setSelectedExecution(execution)

      // Fetch Claude logs for this execution from server with filters
      try {
        // Convert execution times to Unix epoch seconds (server expects seconds)
        const startTimeSeconds = new Date(execution.start_time).getTime() / 1000
        const endTimeSeconds = execution.end_time ? new Date(execution.end_time).getTime() / 1000 : Date.now() / 1000

        const params = new URLSearchParams({
          count: '500',
          agent: execution.agent,
          start_time: startTimeSeconds.toString(),
          end_time: endTimeSeconds.toString()
        })

        const response = await fetch(`/claude-logs-history?${params}`)
        const data = await response.json()

        console.log('Claude logs response:', data)
        console.log('Filtered logs for execution:', data.count)
        if (data.logs && data.logs.length > 0) {
          console.log('First log structure:', data.logs[0])
        }

        if (data.success && data.logs) {
          setExecutionLogs(data.logs)
        }
      } catch (error) {
        console.error('Error fetching execution logs:', error)
      }
      return
    }

    // If clicking agent node, expand/collapse executions
    if (!agentName) return

    // Toggle expansion
    if (expandedAgent === agentName) {
      setExpandedAgent(null)
      setExecutionNodes([])
      return
    }

    setExpandedAgent(agentName)

    // Fetch executions for this agent from history
    try {
      const response = await fetch('/history?count=500')
      const data = await response.json()

      if (data.success && data.events) {
        const executionMap = new Map()

        data.events.forEach(event => {
          if (event.agent === agentName) {
            const taskId = event.task_id

            if (!executionMap.has(taskId)) {
              executionMap.set(taskId, {
                task_id: taskId,
                agent: agentName,
                start_time: null,
                end_time: null,
                status: 'unknown',
                duration_ms: null,
                events: []
              })
            }

            const execution = executionMap.get(taskId)
            execution.events.push(event)

            if (event.event_type === 'agent_initialized') {
              execution.start_time = event.timestamp
              execution.status = 'initialized'
            } else if (event.event_type === 'task_received') {
              if (!execution.start_time) execution.start_time = event.timestamp
            } else if (event.event_type === 'agent_completed') {
              execution.end_time = event.timestamp
              execution.status = 'completed'
              execution.duration_ms = event.data?.duration_ms
            } else if (event.event_type === 'agent_failed') {
              execution.end_time = event.timestamp
              execution.status = 'failed'
            }
          }
        })

        const executions = Array.from(executionMap.values())

        // Create execution nodes positioned below the agent node
        const parentNode = nodes.find(n => n.data.agentName === agentName)
        const execNodes = executions.map((execution, index) => ({
          id: `exec-${execution.task_id}`,
          type: 'default',
          position: {
            x: parentNode.position.x + 300,
            y: parentNode.position.y + (index * 80) - ((executions.length - 1) * 40)
          },
          sourcePosition: 'right', // Source on right (hidden but needed)
          targetPosition: 'left', // Connections come in from the left
          data: {
            label: (
              <div style={{ textAlign: 'center' }}>
                <div style={{ fontSize: '11px', fontWeight: 'bold', color: '#fff' }}>
                  {String(execution.task_id).substring(0, 8)}
                </div>
                <div style={{ fontSize: '10px', color: '#d1d5db' }}>
                  {execution.status}
                </div>
              </div>
            ),
            isExecution: true,
            execution: execution
          },
          style: {
            background: execution.status === 'completed' ? '#059669' : execution.status === 'failed' ? '#dc2626' : '#6b7280',
            color: '#fff',
            border: '2px solid #374151',
            borderRadius: '6px',
            padding: '8px',
            width: 120,
            cursor: 'pointer',
          }
        }))

        setExecutionNodes(execNodes)
      }
    } catch (error) {
      console.error('Error fetching agent executions:', error)
    }
  }, [expandedAgent, nodes])

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />

      <div className="flex gap-3 my-3">
        <Link
          to="/"
          className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        >
          <Activity className="inline w-4 h-4 mr-2" />
          Dashboard
        </Link>
        <Link
          to="/pipeline"
          className="px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md text-sm text-white"
        >
          <GitBranch className="inline w-4 h-4 mr-2" />
          Pipeline View
        </Link>
        <button
          onClick={fetchPipeline}
          className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm ml-auto"
        >
          <RefreshCw className="inline w-4 h-4 mr-2" />
          Refresh
        </button>
      </div>

      <div className="flex gap-4">
        {/* Pipeline Flow Chart */}
        <div className={`bg-gh-canvas-subtle rounded-md border border-gh-border p-4 mb-4 transition-all ${selectedExecution ? 'w-2/3' : 'w-full'}`}>
          <div className="flex justify-between items-center mb-4">
            <div>
              <h2 className="text-gh-accent-primary text-lg font-semibold">
                {pipelineData?.pipeline?.pipeline_name || 'No Pipeline'}
              </h2>
              {pipelineData?.pipeline?.project && (
                <p className="text-gh-fg-muted text-sm mt-1">
                  Project: {String(pipelineData.pipeline.project)}
                  {pipelineData?.pipeline?.issue_number && ` • Issue #${pipelineData.pipeline.issue_number}`}
                </p>
              )}
            </div>
            <div className="text-right">
              {pipelineData?.pipeline?.active ? (
                <span className="px-3 py-1 bg-green-600 text-white text-xs rounded-full font-semibold">
                  Active
                </span>
              ) : (
                <span className="px-3 py-1 bg-gh-border text-gh-fg-muted text-xs rounded-full">
                  Idle
                </span>
              )}
              {pipelineData?.pipeline?.progress !== undefined && (
                <p className="text-gh-accent-primary text-sm mt-2 font-semibold">
                  Progress: {pipelineData.pipeline.progress}%
                </p>
              )}
            </div>
          </div>
          {loading ? (
            <p className="text-gh-fg-muted text-sm mt-1">Loading...</p>
          ) : pipelineData?.pipeline?.stages && allNodes.length > 0 ? (
            <div className="mt-4" style={{ height: '600px' }}>
              <ReactFlow
                nodes={allNodes}
                edges={allEdges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={onNodeClick}
                fitView
                fitViewOptions={{ padding: 0.2 }}
              >
                <Background />
                <Controls />
                <MiniMap />
              </ReactFlow>
            </div>
          ) : (
            <p className="text-gh-fg-muted text-sm mt-1">No stages available</p>
          )}
        </div>

        {/* Execution Logs Side Panel */}
        {selectedExecution && (
          <div className="w-1/3 bg-gh-canvas-subtle rounded-md border border-gh-border p-4 mb-4">
            <div className="flex justify-between items-center mb-4">
              <div>
                <h3 className="text-gh-accent-primary text-lg font-semibold">Execution Logs</h3>
                <p className="text-sm text-gh-fg-muted mt-1">
                  {selectedExecution.agent}
                </p>
                <p className="text-xs text-gh-fg-muted">
                  {String(selectedExecution.task_id).substring(0, 12)}...
                </p>
              </div>
              <button
                onClick={() => {
                  setSelectedExecution(null)
                  setExecutionLogs([])
                }}
                className="text-gh-fg-muted hover:text-gh-fg"
              >
                ✕
              </button>
            </div>

            <div ref={logsContainerRef} className="overflow-y-auto font-mono text-xs" style={{ height: '550px' }}>
              {executionLogs.length === 0 ? (
                <p className="p-4 text-center text-gh-fg-muted">No logs found for this execution</p>
              ) : (
                executionLogs.map((log, idx) => {
                  const { logType, logContent } = getLogContent(log)
                  if (!logContent) return null

                  return (
                    <div
                      key={idx}
                      className="flex gap-3 p-2 border-b border-gh-border-muted hover:bg-gh-canvas transition-colors items-start"
                    >
                      <span className="text-gh-fg-subtle whitespace-nowrap">
                        {log.timestamp ? new Date(typeof log.timestamp === 'number' ? log.timestamp * 1000 : log.timestamp).toLocaleTimeString('en-US', { timeZone: 'UTC', hour12: false }) + ' UTC' : ''}
                      </span>
                      <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase whitespace-nowrap ${getLogTypeColor(logType)} text-white`}>
                        {logType}
                      </span>
                      <div className="flex-1 min-w-0">
                        {logType === 'text' ? (
                          <div className="prose prose-sm prose-invert max-w-none">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>
                              {logContent}
                            </ReactMarkdown>
                          </div>
                        ) : (
                          <span className="break-words">{logContent}</span>
                        )}
                      </div>
                    </div>
                  )
                })
              )}
            </div>
          </div>
        )}
      </div>

    </div>
  )
}

export const Route = createFileRoute('/pipeline')({
  component: PipelineView,
})
