import { createFileRoute } from '@tanstack/react-router'
import { RefreshCw } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import PipelineFlowGraph from '../components/PipelineFlowGraph'
import PipelineRunEventLog from '../components/PipelineRunEventLog'
import PipelineRunSidebar from '../components/PipelineRunSidebar'
import PipelineRunHeader from '../components/PipelineRunHeader'
import ConfirmationModal from '../components/ConfirmationModal'
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useSocket } from '../contexts/SocketContext'
import { toggleCycleCollapsed } from '../utils/cycleLayout'
import { mergePipelineRunEvents, mergeArrayByIdStable } from '../utils/eventMerging'
import { buildFlowchart as buildFlowchartUtil, findActiveContainerPath } from '../utils/buildFlowchart'
import { processEvents } from '../utils/eventProcessing/index.js'

function PipelineRunView() {
  const navigate = useNavigate()
  const searchParams = Route.useSearch()
  const urlRunId = searchParams.runId
  const contentTab = searchParams.contentTab

  const [activePipelineRuns, setActivePipelineRuns] = useState([])
  const [completedPipelineRuns, setCompletedPipelineRuns] = useState([])
  const [selectedPipelineRun, setSelectedPipelineRun] = useState(null)
  const [pipelineRunEvents, setPipelineRunEvents] = useState([])
  const [workflowConfig, setWorkflowConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingCompleted, setLoadingCompleted] = useState(false)
  const [loadingEvents, setLoadingEvents] = useState(false)
  const [completedLoadedCount, setCompletedLoadedCount] = useState(0)
  const [hasMoreCompleted, setHasMoreCompleted] = useState(true)
  const [rawBuild, setRawBuild] = useState(null)
  const [cycles, setCycles] = useState(new Map())
  const cyclesRef = useRef(new Map())
  const [userOpenedCycles, setUserOpenedCycles] = useState(new Set())
  const [showKillModal, setShowKillModal] = useState(false)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [activeFilters, setActiveFilters] = useState({ project: '', board: '', outcome: '' })
  const [filterOptions, setFilterOptions] = useState({ projects: [], boards: [], outcomes: [] })
  const completedLimit = 10
  const { events: socketEvents } = useSocket()

  // Refs to track state without causing re-renders
  const selectedPipelineRunRef = useRef(selectedPipelineRun)
  const isFetchingActiveRef = useRef(false)
  const isFetchingCompletedRef = useRef(false)
  const completedLoadedCountRef = useRef(0)
  const socketEventsRef = useRef(socketEvents)
  const previousActiveRunIdsRef = useRef(new Set())
  const processedEventIdsRef = useRef(new Set())
  const lastProcessedTimestampRef = useRef(0)
  const activeFiltersRef = useRef(activeFilters)
  const urlRunIdRef = useRef(urlRunId)
  const prevUrlRunIdRef = useRef(urlRunId)
  const userOpenedCyclesRef = useRef(userOpenedCycles)
  const prevAutoOpenedRef = useRef(new Set())

  useEffect(() => { selectedPipelineRunRef.current = selectedPipelineRun }, [selectedPipelineRun])
  useEffect(() => { completedLoadedCountRef.current = completedLoadedCount }, [completedLoadedCount])
  useEffect(() => { socketEventsRef.current = socketEvents }, [socketEvents])
  useEffect(() => { activeFiltersRef.current = activeFilters }, [activeFilters])
  useEffect(() => { urlRunIdRef.current = urlRunId }, [urlRunId])
  useEffect(() => { userOpenedCyclesRef.current = userOpenedCycles }, [userOpenedCycles])
  useEffect(() => { cyclesRef.current = cycles }, [cycles])

  // Re-fetch completed runs when filters change (also handles initial mount)
  useEffect(() => {
    setCompletedPipelineRuns([])
    setCompletedLoadedCount(0)
    setHasMoreCompleted(true)
    setFilterOptions({ projects: [], boards: [], outcomes: [] })
    fetchCompletedPipelineRuns(0, false, null, activeFilters)
  }, [activeFilters]) // eslint-disable-line react-hooks/exhaustive-deps

  // URL navigation helpers
  const updateUrlParams = useCallback((updates, replaceHistory = true) => {
    navigate({
      search: (prev) => ({ ...prev, ...updates }),
      replace: replaceHistory,
    })
  }, [navigate])

  const handleSelectRun = useCallback((run) => {
    updateUrlParams({ runId: run.id }, false)
  }, [updateUrlParams])

  const handleDeselectRun = useCallback(() => {
    updateUrlParams({ runId: undefined }, true)
  }, [updateUrlParams])

  // Sync selectedPipelineRun from URL
  useEffect(() => {
    if (urlRunId) {
      const run = activePipelineRuns.find(r => r.id === urlRunId)
        || completedPipelineRuns.find(r => r.id === urlRunId)

      if (run) {
        setSelectedPipelineRun(run)
      } else if (!loading && !loadingCompleted) {
        // Only deselect once both fetches have settled — "not found" during
        // initial load just means the data hasn't arrived yet.
        setSelectedPipelineRun(null)
        handleDeselectRun()
      }
    } else {
      if (!selectedPipelineRunRef.current) {
        const firstRun = activePipelineRuns[0] || completedPipelineRuns[0]
        if (firstRun) {
          setSelectedPipelineRun(firstRun)
          updateUrlParams({ runId: firstRun.id }, true)
        }
      }
    }
  }, [urlRunId, activePipelineRuns, completedPipelineRuns, updateUrlParams, handleDeselectRun, loading, loadingCompleted])

  // Track previous active run IDs (for transition detection)
  useEffect(() => {
    previousActiveRunIdsRef.current = new Set(activePipelineRuns.map(r => r.id))
  }, [activePipelineRuns])

  // Fetch active pipeline runs — stable (no deps), ref-guarded
  const fetchActivePipelineRuns = useCallback(async (isInitialLoad = false) => {
    if (isFetchingActiveRef.current) return

    try {
      isFetchingActiveRef.current = true
      if (isInitialLoad) setLoading(true)

      const response = await fetch('/active-pipeline-runs')
      const data = await response.json()

      if (data.success) {
        setActivePipelineRuns(currentRuns => {
          const mergedRuns = mergeArrayByIdStable(currentRuns, data.runs)
          setSelectedPipelineRun(currentSelected => {
            if (currentSelected) {
              const updatedRun = mergedRuns.find(run => run.id === currentSelected.id)
              if (updatedRun) return updatedRun
            }
            return currentSelected
          })
          return mergedRuns
        })
      }
    } catch (error) {
      console.error('Error fetching active pipeline runs:', error)
    } finally {
      if (isInitialLoad) setLoading(false)
      isFetchingActiveRef.current = false
    }
  }, [])

  // Fetch completed pipeline runs — stable, ref-guarded
  const fetchCompletedPipelineRuns = useCallback(async (offset = 0, append = false, customLimit = null, filters = null) => {
    if (isFetchingCompletedRef.current) return

    const resolvedFilters = filters ?? activeFiltersRef.current

    try {
      isFetchingCompletedRef.current = true
      setLoadingCompleted(true)

      const limit = customLimit || completedLimit
      const params = new URLSearchParams({ limit, offset })
      if (resolvedFilters.project) params.set('project', resolvedFilters.project)
      if (resolvedFilters.board) params.set('board', resolvedFilters.board)
      if (resolvedFilters.outcome) params.set('outcome', resolvedFilters.outcome)
      const response = await fetch(`/completed-pipeline-runs?${params}`)
      const data = await response.json()

      if (data.success) {
        let isInitialLoad = false

        setCompletedPipelineRuns(currentCompleted => {
          isInitialLoad = currentCompleted.length === 0 && data.runs.length > 0

          let mergedRuns
          if (append) {
            const combined = [...currentCompleted, ...data.runs]
            mergedRuns = mergeArrayByIdStable(currentCompleted, combined)
          } else {
            mergedRuns = mergeArrayByIdStable(currentCompleted, data.runs)
          }

          setSelectedPipelineRun(currentSelected => {
            if (currentSelected) {
              const updatedRun = mergedRuns.find(run => run.id === currentSelected.id)
              if (updatedRun) return updatedRun
            }
            return currentSelected
          })

          return mergedRuns
        })

        // Accumulate filter option values from returned runs
        setFilterOptions(prev => ({
          projects: [...new Set([...prev.projects, ...data.runs.map(r => r.project).filter(Boolean)])].sort(),
          boards: [...new Set([...prev.boards, ...data.runs.map(r => r.board).filter(Boolean)])].sort(),
          outcomes: [...new Set([
            ...prev.outcomes,
            ...data.runs.map(r => r.outcome).filter(Boolean),
            ...(data.runs.some(r => !r.outcome) ? ['unknown'] : []),
          ])].sort(),
        }))

        if (append) {
          setCompletedLoadedCount(prev => prev + data.runs.length)
          setHasMoreCompleted(data.runs.length === completedLimit)
        } else if (isInitialLoad) {
          setCompletedLoadedCount(data.runs.length)
          setHasMoreCompleted(data.runs.length === completedLimit)
        }
      }
    } catch (error) {
      console.error('Error fetching completed pipeline runs:', error)
    } finally {
      setLoadingCompleted(false)
      isFetchingCompletedRef.current = false
    }
  }, [completedLimit])

  const loadMoreCompleted = useCallback(() => {
    fetchCompletedPipelineRuns(completedLoadedCount, true, null, activeFiltersRef.current)
  }, [completedLoadedCount, fetchCompletedPipelineRuns])

  // Fetch workflow configuration
  const fetchWorkflowConfig = useCallback(async (project, board) => {
    if (!project || !board) return
    try {
      const response = await fetch(`/api/workflow-config/${project}/${board}`)
      const data = await response.json()
      if (data.success) {
        setWorkflowConfig(data.workflow)
      } else {
        setWorkflowConfig(null)
      }
    } catch (error) {
      console.error('Error fetching workflow configuration:', error)
      setWorkflowConfig(null)
    }
  }, [])

  // Fetch events for selected pipeline run.
  // Pass { silent: true } for background refreshes to avoid showing the spinner
  // and hiding the graph — only the initial load per run should set loadingEvents.
  const fetchPipelineRunEvents = useCallback(async (pipelineRunId, { silent = false } = {}) => {
    if (!pipelineRunId) return
    try {
      if (!silent) setLoadingEvents(true)
      const response = await fetch(`/pipeline-run-events?pipeline_run_id=${pipelineRunId}`)
      const data = await response.json()

      if (data.success && urlRunIdRef.current === pipelineRunId) {
        const newEventsWithKeys = data.events.map((event, idx) => ({
          ...event,
          _key: event.id || `${event.timestamp}_${idx}`
        }))
        setPipelineRunEvents(newEventsWithKeys)
      }
    } catch (error) {
      console.error('Error fetching pipeline run events:', error)
    } finally {
      if (!silent) setLoadingEvents(false)
    }
  }, [])

  const handleKillRun = useCallback(() => {
    if (!selectedPipelineRun) return
    setShowKillModal(true)
  }, [selectedPipelineRun])

  const confirmKillRun = useCallback(async () => {
    if (!selectedPipelineRun) return
    try {
      const response = await fetch(`/pipeline-runs/${selectedPipelineRun.id}/kill`, { method: 'POST' })
      const data = await response.json()
      if (data.success) {
        fetchActivePipelineRuns()
        fetchCompletedPipelineRuns(0, false, null, activeFiltersRef.current)
      } else {
        alert(`Failed to kill run: ${data.error}`)
      }
    } catch (error) {
      console.error('Error killing pipeline run:', error)
      alert('Error killing pipeline run')
    }
  }, [selectedPipelineRun, fetchActivePipelineRuns, fetchCompletedPipelineRuns])

  const toggleFullscreen = useCallback(() => setIsFullscreen(prev => !prev), [])

  const handleToggleCycle = useCallback((cycleId) => {
    const currentIsCollapsed = cyclesRef.current.get(cycleId)?.isCollapsed ?? true
    setCycles(prev => toggleCycleCollapsed(prev, cycleId))
    setUserOpenedCycles(prev => {
      const next = new Set(prev)
      if (currentIsCollapsed) next.add(cycleId)   // user expanding → remember
      else next.delete(cycleId)                    // user collapsing → forget
      return next
    })
  }, [])

  // Merge API events with live WebSocket events (full, unfiltered)
  const mergedEvents = useMemo(() => {
    if (!selectedPipelineRun) return []
    return mergePipelineRunEvents(pipelineRunEvents, socketEvents, selectedPipelineRun)
  }, [pipelineRunEvents, socketEvents, selectedPipelineRun])

  // Graph events: exclude claude_log streaming events — never used for cycle detection
  // or node building. Large runs have 80%+ claude_log events; filtering here prevents
  // processEvents / buildFlowchart from sorting and scanning thousands of irrelevant events.
  const graphEvents = useMemo(() =>
    mergedEvents.filter(e => e.event_category !== 'claude_log'),
    [mergedEvents]
  )

  // Filtered events for event log tab (exclude Claude streaming)
  const eventLogEvents = useMemo(() => {
    const claudeStreamEventTypes = new Set([
      'claude_stream', 'claude_stream_event', 'text_output', 'text_delta',
      'tool_call', 'tool_use', 'tool_result', 'input_json_delta',
      'message_start', 'message_delta', 'message_stop',
      'content_block_start', 'content_block_delta', 'content_block_stop',
    ])
    return mergedEvents.filter(event => {
      if (event.event_category === 'claude_api') return false
      if (claudeStreamEventTypes.has(event.event_type)) return false
      if (event.raw_event) {
        const rawEventType = event.raw_event?.event?.type || event.raw_event?.type
        if (claudeStreamEventTypes.has(rawEventType)) return false
      }
      return true
    })
  }, [mergedEvents])

  // Find latest agent execution ID — graphEvents is sorted ascending, so the last
  // agent_initialized event is the most recently started agent execution.
  const latestAgentExecutionId = useMemo(() => {
    if (!graphEvents || graphEvents.length === 0) return null
    const agentInitEvents = graphEvents.filter(event => event.event_type === 'agent_initialized')
    if (agentInitEvents.length === 0) return null
    const ev = agentInitEvents.at(-1)
    return ev.agent_execution_id || ev.data?.agent_execution_id || null
  }, [graphEvents])

  // Determine if currently in conversational loop
  const isConversational = useMemo(() => {
    if (!graphEvents || graphEvents.length === 0) return false
    const sortedEvents = [...graphEvents].sort((a, b) =>
      new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
    )
    for (const event of sortedEvents) {
      if (['conversational_loop_started', 'feedback_listening_started', 'conversational_loop_resumed'].includes(event.event_type)) {
        return true
      }
      if (['feedback_listening_stopped', 'conversational_loop_paused', 'pipeline_stage_transition', 'agent_completed'].includes(event.event_type)) {
        return false
      }
    }
    return false
  }, [graphEvents])

  // Throttle ref for rawBuild updates (≤ 2/sec during heavy socket activity)
  const rawBuildThrottleRef = useRef({ timer: null, lastRun: 0, pendingFn: null })

  // Build raw (unpositioned) flowchart — pure, returns result
  const buildRawFlowchart = useCallback(() => {
    if (!graphEvents.length || !selectedPipelineRun) return null

    // Compute active task_ids from graphEvents (API + socket merged stream) so that
    // historical agent_initialized events — which predate the WebSocket connection —
    // are included. Task_id matching also avoids false positives when the same agent
    // type has run multiple times.
    const agentTaskInit = new Map()
    const agentTaskDone = new Set()
    graphEvents.forEach(e => {
      if (e.event_type === 'agent_initialized') agentTaskInit.set(e.task_id, e.agent)
      else if (e.event_type === 'agent_completed' || e.event_type === 'agent_failed') agentTaskDone.add(e.task_id)
    })
    const activeTaskIds = new Set()
    agentTaskInit.forEach((agent, taskId) => {
      if (!agentTaskDone.has(taskId)) activeTaskIds.add(taskId)
    })

    return buildFlowchartUtil({
      events: graphEvents,
      existingCycles: cyclesRef.current,
      workflowConfig,
      selectedPipelineRun,
      activeTaskIds,
    })
  }, [graphEvents, selectedPipelineRun, workflowConfig])

  const handleDownloadDebugData = useCallback(() => {
    if (!selectedPipelineRun) return
    const debugData = {
      pipelineRun: selectedPipelineRun,
      events: pipelineRunEvents,
      workflowConfig,
      cycles: Array.from(cycles.entries()).map(([id, data]) => ({
        id,
        ...data,
        agentExecutions: data.agentExecutions?.map(e => ({
          agent: e.agent,
          taskId: e.execution?.taskId || e.taskId,
        })),
      })),
      timestamp: new Date().toISOString(),
    }
    const blob = new Blob([JSON.stringify(debugData, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `pipeline-run-${selectedPipelineRun.id.substring(0, 8)}-debug.json`
    a.click()
    URL.revokeObjectURL(url)
  }, [selectedPipelineRun, pipelineRunEvents, workflowConfig, cycles])

  // Initial load (completed runs handled by the activeFilters effect above)
  useEffect(() => {
    fetchActivePipelineRuns(true)
  }, [fetchActivePipelineRuns])

  // Periodic refresh every 10 seconds
  useEffect(() => {
    const intervalId = setInterval(() => {
      fetchActivePipelineRuns(false)
      if (completedLoadedCountRef.current > 0) {
        const itemsToRefresh = Math.max(completedLoadedCountRef.current, completedLimit)
        fetchCompletedPipelineRuns(0, false, itemsToRefresh, activeFiltersRef.current)
      }
    }, 10000)
    return () => clearInterval(intervalId)
  }, [fetchActivePipelineRuns, fetchCompletedPipelineRuns, completedLimit])

  const selectedRunId = selectedPipelineRun?.id

  // Reset user-opened tracking when switching pipeline runs
  useEffect(() => {
    setUserOpenedCycles(new Set())
    prevAutoOpenedRef.current = new Set()
  }, [selectedRunId])

  // Prefetch events immediately from the URL param — don't wait for run metadata
  // to resolve from the run list. This eliminates the blank period on page reload.
  // Clear events when switching to a *different* run so stale graph/log data from
  // the previous run is not shown while the new fetch is in flight.
  useEffect(() => {
    if (!urlRunId) return
    if (urlRunId !== prevUrlRunIdRef.current) {
      setPipelineRunEvents([])
      prevUrlRunIdRef.current = urlRunId
    }
    fetchPipelineRunEvents(urlRunId)
  }, [urlRunId, fetchPipelineRunEvents])

  // Load workflow config when run selection changes (keyed on id to avoid refetching on metadata updates).
  // Events are handled by the urlRunId effect above — don't clear or re-fetch here to avoid
  // a flash of empty content when switching between runs.
  useEffect(() => {
    if (selectedRunId) {
      const run = selectedPipelineRunRef.current
      if (run) fetchWorkflowConfig(run.project, run.board)
    }
  }, [selectedRunId, fetchWorkflowConfig])

  // Periodic polling for active run events — silent fallback every 30s if socket events are missed.
  // WebSocket is the primary update path; this only catches gaps in socket delivery.
  // Status is checked inside the callback via ref so this effect is not coupled to
  // activePipelineRuns — the timer runs a stable 30s cycle keyed only on selectedRunId.
  useEffect(() => {
    if (!selectedRunId) return

    const intervalId = setInterval(() => {
      if (selectedPipelineRunRef.current?.status === 'active') {
        fetchPipelineRunEvents(selectedRunId, { silent: true })
      }
    }, 30000)

    return () => clearInterval(intervalId)
  }, [selectedRunId, fetchPipelineRunEvents])

  // Detect and update cycles when events change; auto-expand hierarchy path to active agent
  useEffect(() => {
    if (!graphEvents.length) return

    console.log(`[PerfGraph] cycles-effect START (${graphEvents.length} graphEvents)`)
    const _ceT0 = performance.now()
    const model = processEvents(graphEvents)
    console.log(`[PerfGraph] cycles-effect processEvents: ${(performance.now() - _ceT0).toFixed(1)}ms → ${model.cycles.length} cycles`)

    // Determine active agents: initialized but not yet completed/failed
    const agentTaskInit = new Map()
    const agentTaskDone = new Set()
    graphEvents.forEach(e => {
      if (e.event_type === 'agent_initialized') agentTaskInit.set(e.task_id, e.agent)
      else if (e.event_type === 'agent_completed' || e.event_type === 'agent_failed') agentTaskDone.add(e.task_id)
    })
    const activeTaskIds = new Set()
    agentTaskInit.forEach((agent, taskId) => {
      if (!agentTaskDone.has(taskId)) activeTaskIds.add(taskId)
    })

    // Find containers that lead to the active agent
    const activeContainerIds = findActiveContainerPath(model, activeTaskIds)
    const prevAutoOpened = prevAutoOpenedRef.current

    // Build new top-level cycle map (all expanded by default)
    const newCycleMap = new Map()
    model.cycles.forEach(cycle => {
      newCycleMap.set(cycle.id, { isCollapsed: false })
    })

    // Compute the merged cycle map directly (reading from ref avoids the functional
    // update pattern so we can update cyclesRef.current before calling setCycles).
    // Effects fire in definition order: this effect (line ~484) runs before the
    // rebuild effect (line ~548), so cyclesRef.current will already hold the correct
    // value by the time buildRawFlowchart reads it — eliminating the interstitial
    // wrong-isCollapsed state that caused container label/chevron flicker.
    const prevCycles = cyclesRef.current
    const merged = new Map(newCycleMap)
    prevCycles.forEach((prev, id) => {
      if (merged.has(id)) merged.get(id).isCollapsed = prev.isCollapsed
      else merged.set(id, prev)  // preserve nested container states
    })

    // Auto-expand containers on the active agent path
    activeContainerIds.forEach(id => {
      const current = merged.get(id)
      if (!current || current.isCollapsed) {
        merged.set(id, { ...(current ?? {}), isCollapsed: false })
      }
    })

    // Auto-collapse previously auto-opened containers no longer on the active path,
    // unless the user explicitly opened them
    prevAutoOpened.forEach(id => {
      if (!activeContainerIds.has(id) && !userOpenedCyclesRef.current.has(id)) {
        const current = merged.get(id)
        if (current && !current.isCollapsed) {
          merged.set(id, { ...current, isCollapsed: true })
        }
      }
    })

    cyclesRef.current = merged        // update ref before setCycles
    prevAutoOpenedRef.current = activeContainerIds
    setCycles(merged)
  }, [graphEvents, selectedPipelineRun])

  // Rebuild flowchart when events or config change — throttled to ≤ 2/sec
  useEffect(() => {
    const throttle = rawBuildThrottleRef.current
    throttle.pendingFn = buildRawFlowchart  // always points to latest fn

    const now = Date.now()
    const elapsed = now - throttle.lastRun

    if (elapsed >= 500) {
      // Leading edge — apply immediately
      throttle.lastRun = now
      const _t0 = performance.now()
      const result = buildRawFlowchart() ?? null
      console.log(`[PerfGraph] buildRawFlowchart (leading): ${(performance.now() - _t0).toFixed(1)}ms → nodes:${result?.nodes?.length ?? 0} edges:${result?.edges?.length ?? 0}`)
      setRawBuild(result)
    } else if (!throttle.timer) {
      // Within window — schedule trailing update
      throttle.timer = setTimeout(() => {
        throttle.timer = null
        throttle.lastRun = Date.now()
        const _t0 = performance.now()
        const result = throttle.pendingFn?.() ?? null
        console.log(`[PerfGraph] buildRawFlowchart (trailing): ${(performance.now() - _t0).toFixed(1)}ms → nodes:${result?.nodes?.length ?? 0} edges:${result?.edges?.length ?? 0}`)
        setRawBuild(result)
      }, 500 - elapsed)
    }
    // else: timer already scheduled; pendingFn updated above
  }, [buildRawFlowchart])

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      const { timer } = rawBuildThrottleRef.current
      if (timer) clearTimeout(timer)
    }
  }, [])

  // Update on socket events
  useEffect(() => {
    if (socketEvents.length > 0) {
      const latestEvent = socketEvents[0]
      const eventId = latestEvent.event_id || `${latestEvent.timestamp}_${latestEvent.event_type}`

      if (processedEventIdsRef.current.has(eventId)) return

      let eventTimestamp = 0
      if (latestEvent.timestamp) {
        if (typeof latestEvent.timestamp === 'number') {
          eventTimestamp = latestEvent.timestamp > 10000000000
            ? latestEvent.timestamp / 1000
            : latestEvent.timestamp
        } else if (typeof latestEvent.timestamp === 'string') {
          const date = new Date(latestEvent.timestamp)
          if (!isNaN(date.getTime())) eventTimestamp = date.getTime() / 1000
        }
      }

      if (eventTimestamp <= lastProcessedTimestampRef.current) return

      processedEventIdsRef.current.add(eventId)
      if (processedEventIdsRef.current.size > 1000) {
        const entries = Array.from(processedEventIdsRef.current)
        processedEventIdsRef.current = new Set(entries.slice(-500))
      }
      lastProcessedTimestampRef.current = eventTimestamp

      const pipelineEventTypes = [
        'agent_initialized', 'agent_completed', 'agent_failed',
        'pipeline_stage_started', 'pipeline_stage_completed',
        'review_cycle_started', 'review_cycle_completed',
      ]

      if (pipelineEventTypes.includes(latestEvent.event_type)) {
        fetchActivePipelineRuns(false)
        fetchCompletedPipelineRuns(0, false, null, activeFiltersRef.current)
      }
      // Note: no API refetch here for run events — socket events are merged into
      // mergedEvents directly via mergePipelineRunEvents. The 30s silent poll
      // handles any gaps in socket delivery as a fallback.
    }
  }, [socketEvents, fetchActivePipelineRuns, fetchCompletedPipelineRuns])

  return (
    <div className="h-screen flex flex-col p-5 bg-gh-canvas text-gh-fg">
      {!isFullscreen && <Header />}

      {!isFullscreen && (
        <div className="flex items-center justify-between my-3 flex-shrink-0">
          <NavigationTabs />
          <button
            onClick={() => fetchActivePipelineRuns(true)}
            disabled={loading}
            className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          >
            <RefreshCw className={`inline w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      )}

      <div className={`flex gap-4 ${isFullscreen ? 'fixed inset-0 z-50 p-5 bg-gh-canvas' : 'flex-1 min-h-0'}`}>
        <PipelineRunSidebar
          activePipelineRuns={activePipelineRuns}
          completedPipelineRuns={completedPipelineRuns}
          selectedPipelineRun={selectedPipelineRun}
          loading={loading}
          loadingCompleted={loadingCompleted}
          hasMoreCompleted={hasMoreCompleted}
          onSelectRun={handleSelectRun}
          onLoadMore={loadMoreCompleted}
          activeFilters={activeFilters}
          onFiltersChange={setActiveFilters}
          filterOptions={filterOptions}
        />

        {/* Main Content Area */}
        <div className="relative bg-gh-canvas-subtle border border-gh-border p-4 flex flex-col flex-1 min-h-0 rounded-md">
          {selectedPipelineRun ? (
            <>
              <PipelineRunHeader
                pipelineRun={selectedPipelineRun}
                latestAgentExecutionId={latestAgentExecutionId}
                isConversational={isConversational}
                onKillRun={handleKillRun}
                onDownloadDebugData={handleDownloadDebugData}
                isFullscreen={isFullscreen}
                onToggleFullscreen={toggleFullscreen}
              />

              {/* Content Tab Switcher */}
              <div className="flex border-b border-gh-border mb-4 flex-shrink-0">
                <button
                  onClick={() => updateUrlParams({ contentTab: 'graph' }, true)}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    contentTab === 'graph'
                      ? 'border-gh-accent-emphasis text-gh-accent-fg'
                      : 'border-transparent text-gh-fg-muted hover:text-gh-fg hover:border-gh-border-muted'
                  }`}
                >
                  Graph
                </button>
                <button
                  onClick={() => updateUrlParams({ contentTab: 'log' }, true)}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    contentTab === 'log'
                      ? 'border-gh-accent-emphasis text-gh-accent-fg'
                      : 'border-transparent text-gh-fg-muted hover:text-gh-fg hover:border-gh-border-muted'
                  }`}
                >
                  Event Log
                </button>
              </div>

              {/* Content Area */}
              <div className="flex-1 min-h-0 flex">
                {contentTab === 'graph' ? (
                  <div className="flex-1 min-h-0">
                    <PipelineFlowGraph
                      rawBuild={rawBuild}
                      onToggleCycle={handleToggleCycle}
                      nodesDraggable={selectedPipelineRun.status !== 'active'}
                      allowResizing={selectedPipelineRun.status !== 'active'}
                      minZoom={0.5}
                      maxZoom={1.5}
                      height="100%"
                      loading={loadingEvents}
                      emptyMessage="No events found for this pipeline run"
                      fitViewAlign={selectedPipelineRun.status === 'active' ? 'active-node' : 'top'}
                    />
                  </div>
                ) : (
                  <div className="flex-1 overflow-auto">
                    {loadingEvents ? (
                      <div className="flex items-center justify-center h-32 text-gh-fg-muted text-sm">
                        Loading events…
                      </div>
                    ) : (
                      <PipelineRunEventLog
                        pipelineRun={selectedPipelineRun}
                        events={eventLogEvents}
                        isActive={selectedPipelineRun?.status === 'active'}
                      />
                    )}
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="flex items-center justify-center h-full">
              <p className="text-gh-fg-muted">Select a pipeline run to view details</p>
            </div>
          )}
        </div>
      </div>

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
  validateSearch: (search) => {
    return {
      runId: typeof search.runId === 'string' ? search.runId : undefined,
      contentTab: search.contentTab === 'log' ? 'log' : 'graph',
    }
  },
})
