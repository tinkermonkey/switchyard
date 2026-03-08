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
import { mergePipelineRunEvents, mergeArrayByIdStable } from '../utils/eventMerging'

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
  const previousActiveRunIdsRef = useRef(new Set())
  const processedEventIdsRef = useRef(new Set())
  const lastProcessedTimestampRef = useRef(0)
  const activeFiltersRef = useRef(activeFilters)
  const urlRunIdRef = useRef(urlRunId)
  const prevUrlRunIdRef = useRef(urlRunId)

  useEffect(() => { selectedPipelineRunRef.current = selectedPipelineRun }, [selectedPipelineRun])
  useEffect(() => { completedLoadedCountRef.current = completedLoadedCount }, [completedLoadedCount])
  useEffect(() => { activeFiltersRef.current = activeFilters }, [activeFilters])
  useEffect(() => { urlRunIdRef.current = urlRunId }, [urlRunId])

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

  const handleDownloadDebugData = useCallback(() => {
    if (!selectedPipelineRun) return
    const debugData = {
      pipelineRun: selectedPipelineRun,
      events: pipelineRunEvents,
      workflowConfig,
      timestamp: new Date().toISOString(),
    }
    const blob = new Blob([JSON.stringify(debugData, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `pipeline-run-${selectedPipelineRun.id.substring(0, 8)}-debug.json`
    a.click()
    URL.revokeObjectURL(url)
  }, [selectedPipelineRun, pipelineRunEvents, workflowConfig])

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
                      graphEvents={graphEvents}
                      allEvents={mergedEvents}
                      workflowConfig={workflowConfig}
                      selectedPipelineRun={selectedPipelineRun}
                      nodesDraggable={selectedPipelineRun.status !== 'active'}
                      allowResizing={selectedPipelineRun.status !== 'active'}
                      minZoom={0.5}
                      maxZoom={1.5}
                      height="100%"
                      loading={loadingEvents}
                      emptyMessage="No events found for this pipeline run"
                      fitViewAlign={selectedPipelineRun.status === 'active' ? 'active-node' : 'top'}
                      showAllNodes={false}
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
