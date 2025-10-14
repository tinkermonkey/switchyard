import { createFileRoute } from '@tanstack/react-router'
import { RefreshCw, Activity, CheckCircle, XCircle, AlertCircle, MessageSquare, GitBranch, PlayCircle } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  ReactFlow,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  MarkerType,
  Handle,
  Position,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from '@dagrejs/dagre'
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
        // Color-code by decision category for better visual distinction
        const getDecisionColors = (category) => {
          switch (category) {
            case 'routing':
              return { bg: '#3b82f6', border: '#2563eb' }  // Blue - routing decisions
            case 'progression':
              return { bg: '#10b981', border: '#059669' }  // Green - status progression
            case 'review_cycle':
              return { bg: '#8b5cf6', border: '#7c3aed' }  // Purple - review cycles
            case 'feedback':
              return { bg: '#f59e0b', border: '#d97706' }  // Orange - feedback
            case 'error_handling':
              return { bg: '#ef4444', border: '#dc2626' }  // Red - errors
            case 'task_management':
              return { bg: '#06b6d4', border: '#0891b2' }  // Cyan - task queue
            case 'branch_management':
              return { bg: '#84cc16', border: '#65a30d' }  // Lime - git branches
            case 'conversational_loop':
              return { bg: '#ec4899', border: '#db2777' }  // Pink - conversations
            default:
              return { bg: '#f59e0b', border: '#d97706' }  // Default orange
          }
        }
        
        const colors = getDecisionColors(metadata?.decision_category)
        return {
          ...baseStyle,
          background: colors.bg,
          borderColor: colors.border,
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
  const [completedPipelineRuns, setCompletedPipelineRuns] = useState([])
  const [selectedPipelineRun, setSelectedPipelineRun] = useState(null)
  const [pipelineRunEvents, setPipelineRunEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadingCompleted, setLoadingCompleted] = useState(false)
  const [loadingEvents, setLoadingEvents] = useState(false)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [hoveredNode, setHoveredNode] = useState(null)
  const [selectedTab, setSelectedTab] = useState('active')
  const [completedOffset, setCompletedOffset] = useState(0)
  const [hasMoreCompleted, setHasMoreCompleted] = useState(true)
  const [chartHeight, setChartHeight] = useState(600)
  const [legendOpen, setLegendOpen] = useState(true)
  const [reactFlowInstance, setReactFlowInstance] = useState(null)
  const completedLimit = 10
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
  
  // Fetch completed pipeline runs with pagination
  const fetchCompletedPipelineRuns = useCallback(async (offset = 0, append = false) => {
    try {
      setLoadingCompleted(true)
      const response = await fetch(`/completed-pipeline-runs?limit=${completedLimit}&offset=${offset}`)
      const data = await response.json()
      
      if (data.success) {
        console.log('[PipelineRun] Fetched completed pipeline runs:', data.runs.length)
        if (append) {
          setCompletedPipelineRuns(prev => [...prev, ...data.runs])
        } else {
          setCompletedPipelineRuns(data.runs)
        }
        setHasMoreCompleted(data.runs.length === completedLimit)
        
        // Auto-select first completed run if on completed tab and none selected
        if (!selectedPipelineRun && data.runs.length > 0 && selectedTab === 'completed') {
          setSelectedPipelineRun(data.runs[0])
        }
      }
    } catch (error) {
      console.error('Error fetching completed pipeline runs:', error)
    } finally {
      setLoadingCompleted(false)
    }
  }, [selectedPipelineRun, selectedTab, completedLimit])
  
  // Load more completed runs
  const loadMoreCompleted = useCallback(() => {
    const newOffset = completedOffset + completedLimit
    setCompletedOffset(newOffset)
    fetchCompletedPipelineRuns(newOffset, true)
  }, [completedOffset, completedLimit, fetchCompletedPipelineRuns])
  
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
      setChartHeight(600)
      return
    }
    
    const newNodes = []
    const newEdges = []
    
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
      position: { x: 0, y: 0 }, // Will be positioned by Dagre
      data: {
        label: 'Pipeline Started',
        type: 'pipeline_created',
        metadata: new Date(selectedPipelineRun.started_at).toLocaleString(),
      },
      draggable: false,
    }
    newNodes.push(createdNode)
    
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
        const decisionType = event.event_type || 'decision'
        const reason = event.reason || ''
        
        // Build enhanced metadata
        const metadataParts = []
        
        // Add decision category badge
        if (event.decision_category) {
          metadataParts.push(`[${event.decision_category}]`)
        }
        
        // Add key decision details
        if (event.decision) {
          if (event.decision.selected_agent) metadataParts.push(`→ ${event.decision.selected_agent}`)
          if (event.decision.to_status) metadataParts.push(`→ ${event.decision.to_status}`)
          if (event.decision.action) metadataParts.push(`${event.decision.action}`)
        }
        
        // Add truncated reason
        if (reason) {
          const maxLen = 50
          const truncated = reason.length > maxLen ? reason.substring(0, maxLen) + '...' : reason
          metadataParts.push(truncated)
        }
        
        newNodes.push({
          id: nodeId,
          type: 'pipelineEvent',
          position: { x: 0, y: 0 }, // Will be positioned by Dagre
          data: {
            label: decisionType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
            type: 'decision_event',
            metadata: metadataParts.join(' • '),
            decision_category: event.decision_category,
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
        
        // Check if this is part of a review cycle
        if (reviewCycles.has(agent)) {
          const cycleExecutions = reviewCycles.get(agent)
          const cycleIndex = cycleExecutions.findIndex(e => e.taskId === taskId)
          
          const nodeId = `agent-${agent}-${cycleIndex}`
          
          const execution = cycleExecutions[cycleIndex]
          const isActive = execution.isActive
          
          newNodes.push({
            id: nodeId,
            type: 'pipelineEvent',
            position: { x: 0, y: 0 }, // Will be positioned by Dagre
            data: {
              label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
              type: 'agent_execution',
              status: execution.status,
              metadata: `Iteration ${cycleIndex + 1}${isActive ? ' (Running)' : ''}`,
              isActive,
            },
            draggable: false,
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
            position: { x: 0, y: 0 }, // Will be positioned by Dagre
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
    
    // Add pipeline completed node if pipeline is complete
    if (selectedPipelineRun.status === 'completed') {
      const completedNode = {
        id: 'completed',
        type: 'pipelineEvent',
        position: { x: 0, y: 0 }, // Will be positioned by Dagre
        data: {
          label: 'Pipeline Completed',
          type: 'pipeline_completed',
          metadata: selectedPipelineRun.ended_at 
            ? new Date(selectedPipelineRun.ended_at).toLocaleString()
            : '',
        },
        draggable: false,
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
    
    // Apply Dagre layout
    const dagreGraph = new dagre.graphlib.Graph()
    dagreGraph.setDefaultEdgeLabel(() => ({}))
    dagreGraph.setGraph({ 
      rankdir: 'TB', // Top to Bottom
      nodesep: 80, // Horizontal spacing between nodes
      ranksep: 100, // Vertical spacing between ranks
    })
    
    // Estimated node dimensions (will vary based on content)
    const nodeWidth = 250
    const nodeHeight = 80
    
    // Add nodes to Dagre graph with estimated dimensions
    newNodes.forEach((node) => {
      dagreGraph.setNode(node.id, { 
        width: nodeWidth, 
        height: nodeHeight 
      })
    })
    
    // Add edges to Dagre graph
    newEdges.forEach((edge) => {
      dagreGraph.setEdge(edge.source, edge.target)
    })
    
    // Calculate layout
    dagre.layout(dagreGraph)
    
    // Find the bounds of the layout to center everything
    let minX = Infinity
    let maxX = -Infinity
    newNodes.forEach((node) => {
      const nodeWithPosition = dagreGraph.node(node.id)
      minX = Math.min(minX, nodeWithPosition.x - nodeWidth / 2)
      maxX = Math.max(maxX, nodeWithPosition.x + nodeWidth / 2)
    })
    
    // Calculate center offset to center the entire graph horizontally
    const graphWidth = maxX - minX
    const centerOffset = -minX + (800 - graphWidth) / 2 // Center in an 800px viewport
    
    // Apply calculated positions to nodes (centering them)
    const layoutedNodes = newNodes.map((node) => {
      const nodeWithPosition = dagreGraph.node(node.id)
      return {
        ...node,
        position: {
          x: nodeWithPosition.x - nodeWidth / 2 + centerOffset,
          y: nodeWithPosition.y - nodeHeight / 2,
        },
      }
    })
    
    // Calculate chart height based on the max Y position
    const maxY = Math.max(...layoutedNodes.map(n => n.position.y + nodeHeight))
    setChartHeight(Math.max(600, maxY + 100)) // Add padding at bottom
    
    setNodes(layoutedNodes)
    setEdges(newEdges)
    
    // Fit view after layout is complete
    if (reactFlowInstance) {
      setTimeout(() => {
        reactFlowInstance.fitView({ padding: 0.05, duration: 300 })
      }, 50)
    }
  }, [pipelineRunEvents, selectedPipelineRun, socketEvents, setNodes, setEdges, reactFlowInstance])
  
  // Initial load
  useEffect(() => {
    fetchActivePipelineRuns()
    fetchCompletedPipelineRuns(0, false)
  }, [])
  
  // Fetch data when tab changes
  useEffect(() => {
    if (selectedTab === 'completed' && completedPipelineRuns.length === 0) {
      fetchCompletedPipelineRuns(0, false)
    }
  }, [selectedTab])
  
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
          <h3 className="text-lg font-semibold mb-3">Pipeline Runs</h3>
          
          {/* Tabs */}
          <div className="flex gap-2 mb-4 border-b border-gh-border">
            <button
              onClick={() => setSelectedTab('active')}
              className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                selectedTab === 'active'
                  ? 'border-gh-accent-emphasis text-gh-accent-fg'
                  : 'border-transparent text-gh-fg-muted hover:text-gh-fg hover:border-gh-border-muted'
              }`}
            >
              Active
            </button>
            <button
              onClick={() => setSelectedTab('completed')}
              className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                selectedTab === 'completed'
                  ? 'border-gh-accent-emphasis text-gh-accent-fg'
                  : 'border-transparent text-gh-fg-muted hover:text-gh-fg hover:border-gh-border-muted'
              }`}
            >
              Completed
            </button>
          </div>
          
          {/* Active Pipeline Runs */}
          {selectedTab === 'active' && (
            <>
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
            </>
          )}
          
          {/* Completed Pipeline Runs */}
          {selectedTab === 'completed' && (
            <>
              {loadingCompleted && completedPipelineRuns.length === 0 ? (
                <p className="text-gh-fg-muted text-sm">Loading...</p>
              ) : completedPipelineRuns.length === 0 ? (
                <div className="text-center py-8">
                  <CheckCircle className="w-8 h-8 mx-auto mb-2 opacity-50" />
                  <p className="text-gh-fg-muted text-sm">No completed pipeline runs</p>
                </div>
              ) : (
                <>
                  <div className="space-y-2">
                    {completedPipelineRuns.map(run => (
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
                          {run.ended_at ? (
                            <>Completed {formatDuration(run.ended_at)} ago</>
                          ) : (
                            <>Started {formatDuration(run.started_at)} ago</>
                          )}
                        </div>
                        {run.duration && (
                          <div className="text-xs mt-1 opacity-75">
                            Duration: {Math.floor(run.duration / 60)}m {Math.floor(run.duration % 60)}s
                          </div>
                        )}
                      </button>
                    ))}
                  </div>
                  
                  {/* Load More Button */}
                  {hasMoreCompleted && (
                    <button
                      onClick={loadMoreCompleted}
                      disabled={loadingCompleted}
                      className="w-full mt-3 px-4 py-2 bg-gh-canvas border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm disabled:opacity-50"
                    >
                      {loadingCompleted ? (
                        <>
                          <RefreshCw className="inline w-4 h-4 mr-2 animate-spin" />
                          Loading...
                        </>
                      ) : (
                        'Load More'
                      )}
                    </button>
                  )}
                </>
              )}
            </>
          )}
        </div>
        
        {/* Pipeline Run Flowchart */}
        <div className="flex-1 bg-gh-canvas-subtle rounded-md border border-gh-border p-4 flex gap-4">
          <div className="flex-1">
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
                  <div style={{ height: `${chartHeight}px` }}>
                    <ReactFlow
                      nodes={nodes}
                      edges={edges}
                      onNodesChange={onNodesChange}
                      onEdgesChange={onEdgesChange}
                      onNodeMouseEnter={onNodeMouseEnter}
                      onNodeMouseLeave={onNodeMouseLeave}
                      onInit={setReactFlowInstance}
                      nodeTypes={nodeTypes}
                      nodesDraggable={false}
                      nodesConnectable={false}
                      fitView
                      fitViewOptions={{ padding: 0.05 }}
                      minZoom={0.5}
                      maxZoom={1.5}
                      zoomOnScroll={false}
                      panOnScroll={false}
                    >
                      <Background />
                      <Controls />
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
          
          {/* Collapsible Legend Panel */}
          <div className={`transition-all duration-300 ${legendOpen ? 'w-64' : 'w-10'} bg-gh-canvas border border-gh-border rounded-md`}>
            <div className="flex items-center justify-between p-3 border-b border-gh-border">
              {legendOpen && <h3 className="text-sm font-semibold">Legend</h3>}
              <button
                onClick={() => setLegendOpen(!legendOpen)}
                className="p-1 hover:bg-gh-canvas-subtle rounded transition-colors"
                title={legendOpen ? 'Collapse legend' : 'Expand legend'}
              >
                {legendOpen ? (
                  <XCircle className="w-4 h-4" />
                ) : (
                  <AlertCircle className="w-4 h-4" />
                )}
              </button>
            </div>
            
            {legendOpen && (
              <div className="p-3 space-y-4 overflow-y-auto" style={{ maxHeight: `${chartHeight}px` }}>
                <div>
                  <h4 className="text-xs font-semibold mb-2 text-gh-fg-muted">Pipeline States</h4>
                  <div className="space-y-2 text-xs">
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#10b981' }}></div>
                      <span>Pipeline Started</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#1f6feb' }}></div>
                      <span>Agent Running</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#238636' }}></div>
                      <span>Agent Completed</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#da3633' }}></div>
                      <span>Agent Failed</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#6366f1' }}></div>
                      <span>Pipeline Completed</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded border-2 flex-shrink-0" style={{ borderColor: '#58a6ff', background: '#1f6feb' }}>
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
                
                <div>
                  <h4 className="text-xs font-semibold mb-2 text-gh-fg-muted">Decision Categories</h4>
                  <div className="space-y-2 text-xs">
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#3b82f6' }}></div>
                      <span>Routing</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#10b981' }}></div>
                      <span>Progression</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#8b5cf6' }}></div>
                      <span>Review Cycle</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#f59e0b' }}></div>
                      <span>Feedback</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#ef4444' }}></div>
                      <span>Error Handling</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#06b6d4' }}></div>
                      <span>Task Management</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#84cc16' }}></div>
                      <span>Branch Management</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded flex-shrink-0" style={{ background: '#ec4899' }}></div>
                      <span>Conversational</span>
                    </div>
                  </div>
                </div>
              </div>
            )}
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
