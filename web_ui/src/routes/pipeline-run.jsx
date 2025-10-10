import { createFileRoute } from '@tanstack/react-router'
import { RefreshCw, Activity, CheckCircle, XCircle, AlertCircle, MessageSquare, GitBranch, PlayCircle } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  ReactFlow,
  MiniMap,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  MarkerType,
  Handle,
  Position,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useSocket } from '../contexts/SocketContext'
import { formatDuration } from '../utils/stateHelpers'

/**
 * Custom node component for pipeline run events with candy-stripe animation
 */
const PipelineEventNode = ({ data }) => {
  const { label, type, status, metadata, isActive } = data
  
  // Determine colors based on type and status
  const getNodeStyle = () => {
    const baseStyle = {
      padding: '12px 16px',
      borderRadius: '8px',
      border: '2px solid',
      minWidth: '200px',
      maxWidth: '300px',
      boxShadow: isActive ? '0 0 10px rgba(88, 166, 255, 0.5)' : '0 2px 4px rgba(0,0,0,0.1)',
    }
    
    switch (type) {
      case 'pipeline_created':
        return {
          ...baseStyle,
          background: '#10b981',
          borderColor: '#059669',
          color: '#fff',
        }
      case 'pipeline_completed':
        return {
          ...baseStyle,
          background: '#6366f1',
          borderColor: '#4f46e5',
          color: '#fff',
        }
      case 'decision_event':
        return {
          ...baseStyle,
          background: '#f59e0b',
          borderColor: '#d97706',
          color: '#fff',
        }
      case 'agent_execution':
        if (status === 'running' || isActive) {
          return {
            ...baseStyle,
            background: '#1f6feb',
            borderColor: '#58a6ff',
            color: '#fff',
            border: '3px solid #58a6ff',
          }
        } else if (status === 'completed') {
          return {
            ...baseStyle,
            background: '#238636',
            borderColor: '#2ea043',
            color: '#fff',
          }
        } else if (status === 'failed') {
          return {
            ...baseStyle,
            background: '#da3633',
            borderColor: '#f85149',
            color: '#fff',
          }
        }
        return {
          ...baseStyle,
          background: '#6e7681',
          borderColor: '#30363d',
          color: '#fff',
        }
      case 'review_feedback':
        return {
          ...baseStyle,
          background: '#8b5cf6',
          borderColor: '#7c3aed',
          color: '#fff',
        }
      case 'human_feedback':
        return {
          ...baseStyle,
          background: '#ec4899',
          borderColor: '#db2777',
          color: '#fff',
        }
      default:
        return {
          ...baseStyle,
          background: '#374151',
          borderColor: '#4b5563',
          color: '#fff',
        }
    }
  }
  
  const getIcon = () => {
    switch (type) {
      case 'pipeline_created':
        return <PlayCircle className="w-4 h-4" />
      case 'pipeline_completed':
        return <CheckCircle className="w-4 h-4" />
      case 'decision_event':
        return <GitBranch className="w-4 h-4" />
      case 'agent_execution':
        if (status === 'completed') return <CheckCircle className="w-4 h-4" />
        if (status === 'failed') return <XCircle className="w-4 h-4" />
        return <Activity className="w-4 h-4" />
      case 'review_feedback':
        return <MessageSquare className="w-4 h-4" />
      case 'human_feedback':
        return <AlertCircle className="w-4 h-4" />
      default:
        return <Activity className="w-4 h-4" />
    }
  }
  
  return (
    <div style={getNodeStyle()} className="relative">
      {/* Candy stripe animation for active agents */}
      {isActive && (
        <div 
          className="absolute top-0 left-0 right-0 h-1 rounded-t-md overflow-hidden"
          style={{
            backgroundImage: 'linear-gradient(45deg, rgba(255,255,255,.2) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.2) 50%, rgba(255,255,255,.2) 75%, transparent 75%, transparent)',
            backgroundSize: '1rem 1rem',
            animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite, stripes 1s linear infinite'
          }}
        />
      )}
      
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      
      <div className="flex items-start gap-2">
        <div className="mt-0.5">{getIcon()}</div>
        <div className="flex-1">
          <div className="font-semibold text-sm">{label}</div>
          {metadata && (
            <div className="text-xs mt-1 opacity-90">
              {metadata}
            </div>
          )}
        </div>
      </div>
      
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

const nodeTypes = {
  pipelineEvent: PipelineEventNode,
}

function PipelineRunView() {
  const [activePipelineRuns, setActivePipelineRuns] = useState([])
  const [selectedPipelineRun, setSelectedPipelineRun] = useState(null)
  const [pipelineRunEvents, setPipelineRunEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadingEvents, setLoadingEvents] = useState(false)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [hoveredNode, setHoveredNode] = useState(null)
  const { events: socketEvents } = useSocket()
  
  // Fetch active pipeline runs
  const fetchActivePipelineRuns = useCallback(async () => {
    try {
      setLoading(true)
      const response = await fetch('/active-pipeline-runs')
      const data = await response.json()
      
      if (data.success) {
        console.log('[PipelineRun] Fetched active pipeline runs:', data.runs)
        data.runs.forEach(run => {
          console.log(`[PipelineRun] Run ${run.id.substring(0, 8)}: started_at=${run.started_at}, status=${run.status}`)
        })
        setActivePipelineRuns(data.runs)
        
        // Auto-select first run if none selected
        if (!selectedPipelineRun && data.runs.length > 0) {
          setSelectedPipelineRun(data.runs[0])
        }
      }
    } catch (error) {
      console.error('Error fetching active pipeline runs:', error)
    } finally {
      setLoading(false)
    }
  }, [selectedPipelineRun])
  
  // Fetch events for selected pipeline run
  const fetchPipelineRunEvents = useCallback(async (pipelineRunId) => {
    if (!pipelineRunId) return
    
    try {
      setLoadingEvents(true)
      const response = await fetch(`/pipeline-run-events?pipeline_run_id=${pipelineRunId}`)
      const data = await response.json()
      
      if (data.success) {
        setPipelineRunEvents(data.events)
      }
    } catch (error) {
      console.error('Error fetching pipeline run events:', error)
    } finally {
      setLoadingEvents(false)
    }
  }, [])
  
  // Build flowchart from events
  const buildFlowchart = useCallback(() => {
    if (!pipelineRunEvents.length || !selectedPipelineRun) {
      setNodes([])
      setEdges([])
      return
    }
    
    const newNodes = []
    const newEdges = []
    let yOffset = 0
    const verticalSpacing = 100
    const horizontalSpacing = 350
    const centerX = 400
    
    // Track currently active agents from socket events
    const activeAgents = new Set()
    socketEvents
      .filter(e => e.event_type === 'agent_initialized')
      .forEach(e => activeAgents.add(e.agent))
    
    socketEvents
      .filter(e => ['agent_completed', 'agent_failed'].includes(e.event_type))
      .forEach(e => activeAgents.delete(e.agent))
    
    // Add pipeline created node
    const createdNode = {
      id: 'created',
      type: 'pipelineEvent',
      position: { x: centerX, y: yOffset },
      data: {
        label: 'Pipeline Started',
        type: 'pipeline_created',
        metadata: new Date(selectedPipelineRun.started_at).toLocaleString(),
      },
    }
    newNodes.push(createdNode)
    yOffset += verticalSpacing
    
    // Track review cycles: map of agent -> [execution instances]
    const reviewCycles = new Map()
    const agentExecutions = new Map()
    
    // First pass: identify all agent executions and group review cycles
    pipelineRunEvents.forEach(event => {
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
            isActive: activeAgents.has(agent),
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
    
    // Identify review cycles (agents with multiple executions)
    agentExecutions.forEach((executions, agent) => {
      if (executions.length > 1) {
        reviewCycles.set(agent, executions)
      }
    })
    
    // Second pass: build nodes and edges chronologically
    let previousNodeId = 'created'
    const processedAgents = new Set()
    
    // Process events chronologically
    const sortedEvents = [...pipelineRunEvents].sort((a, b) => 
      new Date(a.timestamp) - new Date(b.timestamp)
    )
    
    sortedEvents.forEach((event, idx) => {
      let currentNodeId = null
      
      // Decision events
      if (event.event_category === 'decision') {
        const nodeId = `decision-${idx}`
        const decisionType = event.decision_type || 'decision'
        const reason = event.reason || event.description || ''
        
        newNodes.push({
          id: nodeId,
          type: 'pipelineEvent',
          position: { x: centerX, y: yOffset },
          data: {
            label: decisionType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
            type: 'decision_event',
            metadata: reason.substring(0, 60) + (reason.length > 60 ? '...' : ''),
          },
        })
        
        currentNodeId = nodeId
        yOffset += verticalSpacing
      }
      
      // Agent execution starts
      else if (event.event_category === 'agent_lifecycle' && event.event_type === 'agent_initialized') {
        const agent = event.agent
        const taskId = event.task_id
        const executions = agentExecutions.get(agent) || []
        const executionIndex = executions.findIndex(e => e.taskId === taskId)
        
        // Check if this is part of a review cycle
        if (reviewCycles.has(agent)) {
          const cycleExecutions = reviewCycles.get(agent)
          const cycleIndex = cycleExecutions.findIndex(e => e.taskId === taskId)
          
          // Position horizontally for review cycles
          const xPos = centerX + (cycleIndex * horizontalSpacing) - ((cycleExecutions.length - 1) * horizontalSpacing / 2)
          const nodeId = `agent-${agent}-${cycleIndex}`
          
          const execution = cycleExecutions[cycleIndex]
          const isActive = execution.isActive
          
          newNodes.push({
            id: nodeId,
            type: 'pipelineEvent',
            position: { x: xPos, y: yOffset },
            data: {
              label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
              type: 'agent_execution',
              status: execution.status,
              metadata: `Iteration ${cycleIndex + 1}${isActive ? ' (Running)' : ''}`,
              isActive,
            },
          })
          
          // Add feedback edge if this is a retry (not the first execution)
          if (cycleIndex > 0) {
            const previousExecutionId = `agent-${agent}-${cycleIndex - 1}`
            newEdges.push({
              id: `feedback-${agent}-${cycleIndex}`,
              source: previousExecutionId,
              target: nodeId,
              type: 'smoothstep',
              label: 'Revision',
              labelStyle: { fontSize: '10px', fill: '#f59e0b' },
              markerEnd: {
                type: MarkerType.ArrowClosed,
                color: '#f59e0b',
              },
              style: { stroke: '#f59e0b', strokeDasharray: '5,5' },
            })
          }
          
          currentNodeId = nodeId
        } else {
          // Single execution agent
          const nodeId = `agent-${agent}-${executionIndex}`
          const execution = executions[executionIndex]
          const isActive = execution.isActive
          
          newNodes.push({
            id: nodeId,
            type: 'pipelineEvent',
            position: { x: centerX, y: yOffset },
            data: {
              label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
              type: 'agent_execution',
              status: execution.status,
              metadata: isActive ? 'Running' : execution.status,
              isActive,
            },
          })
          
          currentNodeId = nodeId
          yOffset += verticalSpacing
        }
        
        processedAgents.add(agent)
      }
      
      // Connect to previous node
      if (currentNodeId && previousNodeId) {
        newEdges.push({
          id: `edge-${previousNodeId}-${currentNodeId}`,
          source: previousNodeId,
          target: currentNodeId,
          type: 'smoothstep',
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color: '#6e7681',
          },
          style: { stroke: '#6e7681' },
        })
        previousNodeId = currentNodeId
      }
    })
    
    // Advance y offset for review cycles
    if (reviewCycles.size > 0) {
      yOffset += verticalSpacing * 2
    }
    
    // Add pipeline completed node if pipeline is complete
    if (selectedPipelineRun.status === 'completed') {
      const completedNode = {
        id: 'completed',
        type: 'pipelineEvent',
        position: { x: centerX, y: yOffset },
        data: {
          label: 'Pipeline Completed',
          type: 'pipeline_completed',
          metadata: selectedPipelineRun.ended_at 
            ? new Date(selectedPipelineRun.ended_at).toLocaleString()
            : '',
        },
      }
      newNodes.push(completedNode)
      
      // Connect last node to completed
      if (previousNodeId !== 'created') {
        newEdges.push({
          id: `edge-${previousNodeId}-completed`,
          source: previousNodeId,
          target: 'completed',
          type: 'smoothstep',
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color: '#6e7681',
          },
          style: { stroke: '#6e7681' },
        })
      }
    }
    
    setNodes(newNodes)
    setEdges(newEdges)
  }, [pipelineRunEvents, selectedPipelineRun, socketEvents, setNodes, setEdges])
  
  // Initial load
  useEffect(() => {
    fetchActivePipelineRuns()
  }, [])
  
  // Load events when pipeline run selected
  useEffect(() => {
    if (selectedPipelineRun) {
      fetchPipelineRunEvents(selectedPipelineRun.id)
    }
  }, [selectedPipelineRun, fetchPipelineRunEvents])
  
  // Rebuild flowchart when events or socket events change
  useEffect(() => {
    buildFlowchart()
  }, [buildFlowchart])
  
  // Update on socket events
  useEffect(() => {
    if (socketEvents.length > 0) {
      // Refresh pipeline runs and events when new events arrive
      const latestEvent = socketEvents[socketEvents.length - 1]
      if (['agent_initialized', 'agent_completed', 'agent_failed'].includes(latestEvent.event_type)) {
        if (selectedPipelineRun) {
          fetchPipelineRunEvents(selectedPipelineRun.id)
        }
        fetchActivePipelineRuns()
      }
    }
  }, [socketEvents])
  
  const onNodeMouseEnter = useCallback((event, node) => {
    setHoveredNode(node)
  }, [])
  
  const onNodeMouseLeave = useCallback(() => {
    setHoveredNode(null)
  }, [])
  
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      
      <div className="flex items-center justify-between my-3">
        <NavigationTabs />
        <button
          onClick={fetchActivePipelineRuns}
          disabled={loading}
          className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        >
          <RefreshCw className={`inline w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>
      
      <div className="flex gap-4">
        {/* Pipeline Run Selector */}
        <div className="w-64 bg-gh-canvas-subtle rounded-md border border-gh-border p-4">
          <h3 className="text-lg font-semibold mb-3">Active Pipeline Runs</h3>
          
          {loading ? (
            <p className="text-gh-fg-muted text-sm">Loading...</p>
          ) : activePipelineRuns.length === 0 ? (
            <div className="text-center py-8">
              <Activity className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p className="text-gh-fg-muted text-sm">No active pipeline runs</p>
            </div>
          ) : (
            <div className="space-y-2">
              {activePipelineRuns.map(run => (
                <button
                  key={run.id}
                  onClick={() => setSelectedPipelineRun(run)}
                  className={`w-full text-left p-3 rounded border transition-colors ${
                    selectedPipelineRun?.id === run.id
                      ? 'bg-gh-accent-emphasis border-gh-accent-emphasis text-white'
                      : 'bg-gh-canvas border-gh-border hover:border-gh-border-muted'
                  }`}
                >
                  <div className="font-semibold text-sm truncate">
                    {run.issue_title}
                  </div>
                  <div className="text-xs mt-1 opacity-75">
                    {run.project} #{run.issue_number}
                  </div>
                  <div className="text-xs mt-1 opacity-75 font-mono">
                    ID: {run.id.substring(0, 8)}...
                  </div>
                  <div className="text-xs mt-1 opacity-75">
                    Started {formatDuration(run.started_at)} ago
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
        
        {/* Pipeline Run Flowchart */}
        <div className="flex-1 bg-gh-canvas-subtle rounded-md border border-gh-border p-4">
          {selectedPipelineRun ? (
            <>
              <div className="mb-4">
                <h2 className="text-xl font-semibold">{selectedPipelineRun.issue_title}</h2>
                <p className="text-sm text-gh-fg-muted mt-1">
                  {selectedPipelineRun.project} • Issue #{selectedPipelineRun.issue_number} • Board: {selectedPipelineRun.board}
                </p>
                <p className="text-sm text-gh-fg-muted">
                  Started: {new Date(selectedPipelineRun.started_at).toLocaleString()}
                  {selectedPipelineRun.ended_at && ` • Ended: ${new Date(selectedPipelineRun.ended_at).toLocaleString()}`}
                </p>
              </div>
              
              {loadingEvents ? (
                <div className="flex items-center justify-center h-96">
                  <RefreshCw className="w-8 h-8 animate-spin text-gh-accent-primary" />
                </div>
              ) : nodes.length > 0 ? (
                <div style={{ height: '600px' }}>
                  <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onNodeMouseEnter={onNodeMouseEnter}
                    onNodeMouseLeave={onNodeMouseLeave}
                    nodeTypes={nodeTypes}
                    fitView
                    fitViewOptions={{ padding: 0.2 }}
                    minZoom={0.5}
                    maxZoom={1.5}
                  >
                    <Background />
                    <Controls />
                    <MiniMap />
                  </ReactFlow>
                  
                  {/* Hover tooltip */}
                  {hoveredNode && hoveredNode.data.metadata && (
                    <div className="absolute top-20 right-4 bg-gh-canvas-inset border border-gh-border rounded-md p-3 shadow-lg max-w-sm z-10">
                      <div className="font-semibold text-sm mb-1">{hoveredNode.data.label}</div>
                      <div className="text-xs text-gh-fg-muted">{hoveredNode.data.metadata}</div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex items-center justify-center h-96">
                  <p className="text-gh-fg-muted">No events found for this pipeline run</p>
                </div>
              )}
            </>
          ) : (
            <div className="flex items-center justify-center h-96">
              <p className="text-gh-fg-muted">Select a pipeline run to view its flowchart</p>
            </div>
          )}
        </div>
      </div>
      
      {/* Legend */}
      <div className="mt-4 bg-gh-canvas-subtle rounded-md border border-gh-border p-4">
        <h3 className="text-sm font-semibold mb-3">Legend</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs">
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ background: '#10b981' }}></div>
            <span>Pipeline Started</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ background: '#f59e0b' }}></div>
            <span>Decision Event</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ background: '#1f6feb' }}></div>
            <span>Agent Running</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ background: '#238636' }}></div>
            <span>Agent Completed</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ background: '#da3633' }}></div>
            <span>Agent Failed</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ background: '#8b5cf6' }}></div>
            <span>Review Feedback</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ background: '#6366f1' }}></div>
            <span>Pipeline Completed</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded border-2" style={{ borderColor: '#58a6ff', background: '#1f6feb' }}>
              <div style={{
                height: '100%',
                backgroundImage: 'linear-gradient(45deg, rgba(255,255,255,.2) 25%, transparent 25%)',
                backgroundSize: '4px 4px',
              }}></div>
            </div>
            <span>Active (Candy Stripe)</span>
          </div>
        </div>
      </div>
      
      {/* Add CSS for candy stripe animation */}
      <style>{`
        @keyframes stripes {
          0% {
            background-position: 0 0;
          }
          100% {
            background-position: 1rem 1rem;
          }
        }
      `}</style>
    </div>
  )
}

export const Route = createFileRoute('/pipeline-run')({
  component: PipelineRunView,
})
