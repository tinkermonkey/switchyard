import { createFileRoute } from '@tanstack/react-router'
import { RefreshCw, Activity, CheckCircle, XCircle, AlertCircle, Lock, Unlock, Clock } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import CycleBoundingNode from '../components/CycleBoundingNode'
import PipelineEventNode from '../components/PipelineEventNode'
import ReviewCycleContainerNode from '../components/ReviewCycleContainerNode'
import RepairCycleContainerNode from '../components/RepairCycleContainerNode'
import IterationContainerNode from '../components/IterationContainerNode'
import ConfirmationModal from '../components/ConfirmationModal'
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  ReactFlow,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useSocket } from '../contexts/SocketContext'
import { formatDuration } from '../utils/stateHelpers'
import {
  applyCycleLayout,
  toggleCycleCollapsed,
  updateEdgesForCycles,
} from '../utils/cycleLayout'
import { mergePipelineRunEvents, mergeArrayByIdStable } from '../utils/eventMerging'
import { buildFlowchart as buildFlowchartUtil } from '../utils/buildFlowchart'
import { processEvents } from '../utils/eventProcessing/index.js'

const nodeTypes = {
  pipelineEvent: PipelineEventNode,
  cycleBounding: CycleBoundingNode,
  reviewCycleContainer: ReviewCycleContainerNode,
  repairCycleContainer: RepairCycleContainerNode,
  iterationContainer: IterationContainerNode,
}

/**
 * Render lock status badge for a pipeline run
 */
const LockStatusBadge = ({ lockStatus, lockHolderIssue, currentIssue }) => {
  if (lockStatus === 'holding_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-green-400 bg-green-900/20 border border-green-700/30 px-2 py-0.5 rounded">
        <Lock className="w-3 h-3" />
        <span>Holding Lock</span>
      </div>
    )
  } else if (lockStatus === 'waiting_for_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-yellow-400 bg-yellow-900/20 border border-yellow-700/30 px-2 py-0.5 rounded">
        <Clock className="w-3 h-3" />
        <span>Waiting (#{lockHolderIssue})</span>
      </div>
    )
  } else if (lockStatus === 'no_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-blue-400 bg-blue-900/20 border border-blue-700/30 px-2 py-0.5 rounded">
        <Unlock className="w-3 h-3" />
        <span>No Lock</span>
      </div>
    )
  }
  return null
}

function PipelineRunView() {
  const [activePipelineRuns, setActivePipelineRuns] = useState([])
  const [completedPipelineRuns, setCompletedPipelineRuns] = useState([])
  const [selectedPipelineRun, setSelectedPipelineRun] = useState(null)
  const [pipelineRunEvents, setPipelineRunEvents] = useState([])
  const [workflowConfig, setWorkflowConfig] = useState(null) // NEW: Workflow configuration
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
  const [cycles, setCycles] = useState(new Map()) // Track cycle collapse state
  const [showKillModal, setShowKillModal] = useState(false)
  const completedLimit = 10
  const { events: socketEvents } = useSocket()
  const socketEventsRef = useRef(socketEvents)
  useEffect(() => { socketEventsRef.current = socketEvents }, [socketEvents])

  // Fetch active pipeline runs
  const fetchActivePipelineRuns = useCallback(async (isInitialLoad = false) => {
    try {
      // Only show loading spinner on initial load, not on background refreshes
      if (isInitialLoad) {
        setLoading(true)
      }
      const response = await fetch('/active-pipeline-runs')
      const data = await response.json()

      if (data.success) {
        console.log('[PipelineRun] Fetched active pipeline runs:', data.runs)
        data.runs.forEach(run => {
          console.log(`[PipelineRun] Run ${run.id.substring(0, 8)}: started_at=${run.started_at}, status=${run.status}`)
        })
        // Use stable merge to prevent unnecessary re-renders
        setActivePipelineRuns(current => mergeArrayByIdStable(current, data.runs))

        // Auto-select first run if none selected
        if (!selectedPipelineRun && data.runs.length > 0) {
          setSelectedPipelineRun(data.runs[0])
        }

        // If the selected run was active but is no longer in the active list, it has
        // transitioned to completed/failed/killed — fetch its updated object so that
        // status-dependent UI (drag, resize, memoization) updates immediately.
        if (selectedPipelineRun?.status === 'active') {
          const stillActive = data.runs.find(r => r.id === selectedPipelineRun.id)
          if (!stillActive) {
            try {
              const completedRes = await fetch('/completed-pipeline-runs?limit=10&offset=0')
              const completedData = await completedRes.json()
              if (completedData.success) {
                const updated = completedData.runs.find(r => r.id === selectedPipelineRun.id)
                if (updated) {
                  setSelectedPipelineRun(updated)
                }
              }
            } catch (e) {
              // Non-critical: status will sync on next manual refresh
            }
          }
        }
      }
    } catch (error) {
      console.error('Error fetching active pipeline runs:', error)
    } finally {
      if (isInitialLoad) {
        setLoading(false)
      }
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
        // Use stable merge to prevent unnecessary re-renders
        setCompletedPipelineRuns(current => {
          const combinedRuns = append ? [...current, ...data.runs] : data.runs
          return mergeArrayByIdStable(append ? current : [], combinedRuns)
        })
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
  
  // Fetch workflow configuration for selected pipeline run
  const fetchWorkflowConfig = useCallback(async (project, board) => {
    if (!project || !board) return
    
    try {
      const response = await fetch(`/api/workflow-config/${project}/${board}`)
      const data = await response.json()
      
      if (data.success) {
        console.log(`📋 [Pipeline Run] Loaded workflow config for ${project}/${board}:`, data.workflow)
        setWorkflowConfig(data.workflow)
      } else {
        console.error(`Failed to load workflow config: ${data.error}`)
        setWorkflowConfig(null)
      }
    } catch (error) {
      console.error('Error fetching workflow configuration:', error)
      setWorkflowConfig(null)
    }
  }, [])
  
  // Fetch events for selected pipeline run
  const fetchPipelineRunEvents = useCallback(async (pipelineRunId) => {
    if (!pipelineRunId) return
    
    try {
      setLoadingEvents(true)
      const response = await fetch(`/pipeline-run-events?pipeline_run_id=${pipelineRunId}`)
      const data = await response.json()
      
      if (data.success) {
        // Use stable merge for events
        setPipelineRunEvents(current => {
          // Events might not have IDs, so we'll use a composite key
          const eventsWithKeys = data.events.map((event, idx) => ({
            ...event,
            _key: event.id || event.event_id || `${event.timestamp}_${idx}`
          }))
          const currentEventsWithKeys = current.map((event, idx) => ({
            ...event,
            _key: event.id || event.event_id || event._key || `${event.timestamp}_${idx}`
          }))
          return mergeArrayByIdStable(currentEventsWithKeys, eventsWithKeys, '_key')
        })
      }
    } catch (error) {
      console.error('Error fetching pipeline run events:', error)
    } finally {
      setLoadingEvents(false)
    }
  }, [])
  
  // Kill pipeline run
  const handleKillRun = useCallback(() => {
    if (!selectedPipelineRun) return
    setShowKillModal(true)
  }, [selectedPipelineRun])

  const confirmKillRun = useCallback(async () => {
    if (!selectedPipelineRun) return
    
    try {
      const response = await fetch(`/pipeline-runs/${selectedPipelineRun.id}/kill`, {
        method: 'POST'
      })
      const data = await response.json()
      
      if (data.success) {
        // Refresh lists
        fetchActivePipelineRuns()
        fetchCompletedPipelineRuns(0, false)
        // Switch to completed tab since it's now completed/failed
        setSelectedTab('completed')
      } else {
        alert(`Failed to kill run: ${data.error}`)
      }
    } catch (error) {
      console.error('Error killing pipeline run:', error)
      alert('Error killing pipeline run')
    }
  }, [selectedPipelineRun, fetchActivePipelineRuns, fetchCompletedPipelineRuns])

  // Handle cycle collapse/expand toggle
  const handleToggleCycle = useCallback((cycleId) => {
    setCycles(prevCycles => toggleCycleCollapsed(prevCycles, cycleId))
    // State update will trigger useEffect to rebuild flowchart
  }, [])
  
  // Merge API events with live WebSocket events
  const mergedEvents = useMemo(() => {
    if (!selectedPipelineRun) return []
    // Completed runs: don't merge socket events (they're irrelevant and would cause rebuilds)
    if (selectedPipelineRun.status !== 'active') return pipelineRunEvents
    return mergePipelineRunEvents(pipelineRunEvents, socketEvents, selectedPipelineRun)
  }, [pipelineRunEvents, socketEvents, selectedPipelineRun])
  
  // Build flowchart from events
  const buildFlowchart = useCallback(() => {
    if (!mergedEvents.length || !selectedPipelineRun) {
      setNodes([])
      setEdges([])
      setChartHeight(600)
      return
    }

    // Compute active agents from live socket events
    const activeAgents = new Set()
    socketEventsRef.current
      .filter(e => e.event_type === 'agent_initialized')
      .forEach(e => activeAgents.add(e.agent))
    socketEventsRef.current
      .filter(e => ['agent_completed', 'agent_failed'].includes(e.event_type))
      .forEach(e => activeAgents.delete(e.agent))

    // Build raw unpositioned nodes/edges via shared utility
    const { nodes: newNodes, edges: newEdges, agentExecutions, updatedCycles } = buildFlowchartUtil({
      events: mergedEvents,
      existingCycles: cycles,
      workflowConfig,
      selectedPipelineRun,
      activeAgentNames: activeAgents,
    })

    // Apply custom cycle layout
    const { nodes: layoutedNodes } = applyCycleLayout(
      newNodes,
      newEdges,
      updatedCycles,
      {
        nodeWidth: 250,
        nodeHeight: 80,
        horizontalSpacing: 150,
        cycleGap: 100,
        cyclePadding: 40,
        viewportHeight: 600,
      }
    )

    // Update edges for collapsed cycles
    const updatedEdges = updateEdgesForCycles(newEdges, updatedCycles, agentExecutions)

    // Add toggle callback to cycle nodes; enable drag/resize for completed runs
    const isCompleted = selectedPipelineRun?.status !== 'active'
    const CYCLE_CONTAINER_TYPES = ['cycleBounding', 'reviewCycleContainer', 'repairCycleContainer']
    const finalNodes = layoutedNodes.map(node => {
      if (CYCLE_CONTAINER_TYPES.includes(node.type)) {
        return {
          ...node,
          ...(isCompleted && { draggable: true }),
          data: {
            ...node.data,
            onToggleCollapse: handleToggleCycle,
            isResizable: isCompleted && node.type === 'cycleBounding',
          },
        }
      }
      return isCompleted ? { ...node, draggable: true } : node
    })

    // Calculate chart dimensions based on layout
    const maxX = Math.max(...finalNodes.map(n => n.position.x + (n.style?.width || 250)))
    const maxY = Math.max(...finalNodes.map(n => n.position.y + (n.style?.height || 80)))
    setChartHeight(Math.max(600, maxY + 100))

    setNodes(finalNodes)
    setEdges(updatedEdges)

    // Fit view after layout is complete
    if (reactFlowInstance) {
      setTimeout(() => {
        reactFlowInstance.fitView({ padding: 0.1, duration: 300 })
      }, 50)
    }
  }, [mergedEvents, selectedPipelineRun, cycles, workflowConfig, setNodes, setEdges, reactFlowInstance, handleToggleCycle])
  
  // Initial load
  useEffect(() => {
    fetchActivePipelineRuns(true) // Pass true for initial load
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
      // Also fetch workflow configuration
      fetchWorkflowConfig(selectedPipelineRun.project, selectedPipelineRun.board)
    }
  }, [selectedPipelineRun, fetchPipelineRunEvents, fetchWorkflowConfig])
  
  // Detect and update cycles when events change.
  // Uses processEvents to get new-format cycle IDs that match what buildFlowchart
  // produces, so that collapse toggles work correctly.
  useEffect(() => {
    if (!mergedEvents.length) return

    const model = processEvents(mergedEvents)
    const newCycleMap = new Map()
    model.cycles.forEach(cycle => {
      newCycleMap.set(cycle.id, { isCollapsed: false })
    })

    setCycles(prevCycles => {
      const merged = new Map(newCycleMap)
      // Preserve collapse state for any cycle already known
      prevCycles.forEach((prev, id) => {
        if (merged.has(id)) {
          merged.get(id).isCollapsed = prev.isCollapsed
        }
      })
      return merged
    })
  }, [mergedEvents])
  
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
        // Only refetch events for active runs — completed runs don't produce new events
        if (selectedPipelineRun?.status === 'active') {
          fetchPipelineRunEvents(selectedPipelineRun.id)
        }
        fetchActivePipelineRuns(false) // Background refresh from WebSocket event
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
                      {run.lock_status && (
                        <div className="mt-2">
                          <LockStatusBadge
                            lockStatus={run.lock_status}
                            lockHolderIssue={run.lock_holder_issue}
                            currentIssue={run.issue_number}
                          />
                        </div>
                      )}
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
                <div className="mb-4 flex items-start justify-between">
                  <div>
                    <div className="flex items-center gap-3">
                      <h2 className="text-xl font-semibold">{selectedPipelineRun.issue_title}</h2>
                      {selectedPipelineRun.lock_status && (
                        <LockStatusBadge
                          lockStatus={selectedPipelineRun.lock_status}
                          lockHolderIssue={selectedPipelineRun.lock_holder_issue}
                          currentIssue={selectedPipelineRun.issue_number}
                        />
                      )}
                    </div>
                    <p className="text-sm text-gh-fg-muted mt-1">
                      {selectedPipelineRun.project} • Issue #{selectedPipelineRun.issue_number} • Board: {selectedPipelineRun.board}
                    </p>
                    <p className="text-sm text-gh-fg-muted">
                      Started: {new Date(selectedPipelineRun.started_at).toLocaleString()}
                      {selectedPipelineRun.ended_at && ` • Ended: ${new Date(selectedPipelineRun.ended_at).toLocaleString()}`}
                    </p>
                    {selectedPipelineRun.lock_status === 'waiting_for_lock' && selectedPipelineRun.blocked_by_issue && (
                      <div className="mt-2 text-xs text-yellow-400 bg-yellow-900/10 border border-yellow-700/20 px-3 py-2 rounded">
                        ⚠️ This pipeline is waiting for lock currently held by issue #{selectedPipelineRun.blocked_by_issue}
                      </div>
                    )}
                  </div>
                  
                  <div className="flex gap-2">
                    {selectedPipelineRun.status === 'active' && (
                      <button
                        onClick={handleKillRun}
                        className="px-3 py-1 text-xs bg-red-900/20 border border-red-800 text-red-400 rounded hover:bg-red-900/40 transition-colors whitespace-nowrap flex items-center gap-1"
                        title="Kill this pipeline run"
                      >
                        <XCircle className="w-3 h-3" />
                        Kill Run
                      </button>
                    )}
                    
                    {/* Debug: Download Data Button */}
                    <button
                      onClick={() => {
                        const debugData = {
                          pipelineRun: selectedPipelineRun,
                          events: pipelineRunEvents,
                          workflowConfig: workflowConfig,
                          cycles: Array.from(cycles.entries()).map(([id, data]) => ({
                            id,
                            ...data,
                            agentExecutions: data.agentExecutions?.map(e => ({
                              agent: e.agent,
                              taskId: e.execution?.taskId || e.taskId
                            }))
                          })),
                          timestamp: new Date().toISOString()
                        }
                        const blob = new Blob([JSON.stringify(debugData, null, 2)], { type: 'application/json' })
                        const url = URL.createObjectURL(blob)
                        const a = document.createElement('a')
                        a.href = url
                        a.download = `pipeline-run-${selectedPipelineRun.id.substring(0, 8)}-debug.json`
                        a.click()
                        URL.revokeObjectURL(url)
                        console.log('📥 Downloaded debug data')
                      }}
                      className="px-3 py-1 text-xs bg-gh-canvas-subtle border border-gh-border rounded hover:bg-gh-border-muted transition-colors whitespace-nowrap"
                      title="Download debug data as JSON"
                    >
                      📥 Download Debug Data
                    </button>
                  </div>
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
                      nodesDraggable={selectedPipelineRun?.status !== 'active'}
                      nodesConnectable={false}
                      fitView
                      fitViewOptions={{ padding: 0.05 }}
                      minZoom={0.5}
                      maxZoom={1.5}
                      zoomOnScroll={false}
                      panOnScroll={true}
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

      <ConfirmationModal
        show={showKillModal}
        onClose={() => setShowKillModal(false)}
        onConfirm={confirmKillRun}
        title="Kill Pipeline Run"
        message={`Are you sure you want to kill the pipeline run for "${selectedPipelineRun?.issue_title}"? This will stop tracking and mark it as failed.`}
        confirmText="Kill Run"
        isDangerous={true}
      />
    </div>
  )
}

export const Route = createFileRoute('/pipeline-run')({
  component: PipelineRunView,
})
