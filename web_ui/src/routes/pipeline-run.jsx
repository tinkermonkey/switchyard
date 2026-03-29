import { createFileRoute } from '@tanstack/react-router'
import { RefreshCw, List } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import PipelineFlowGraph from '../components/PipelineFlowGraph'
import PromptsFlowGraph from '../components/PromptsFlowGraph'
import PipelineRunEventLog from '../components/PipelineRunEventLog'
import PipelineRunSidebar from '../components/PipelineRunSidebar'
import PipelineRunHeader from '../components/PipelineRunHeader'
import ConfirmationModal from '../components/ConfirmationModal'
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useSocket } from '../contexts/SocketContext'
import { mergePipelineRunEvents, mergeArrayByIdStable } from '../utils/eventMerging'
import { shouldIncludePipelineEvent } from '../components/nodes/EVENT_TYPE_MAP.js'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const PRIORITY_COLORS = {
  high: 'text-red-600 border-red-700/40 bg-white/20',
  medium: 'text-yellow-600 border-yellow-700/40 bg-white/20',
  low: 'text-blue-600 border-blue-700/40 bg-white/20',
}

function PipelineAnalysisReport({ analysis }) {
  const orchRecs = analysis.orchestratorRecommendations || []
  const projRecs = analysis.projectRecommendations || []
  return (
    <div className="flex-1 overflow-auto px-1">
      <div className="space-y-5 py-1">
        {analysis.summary && (
          <div className="prose prose-invert prose-sm max-w-none text-gh-fg-muted text-xs leading-relaxed
            [&_h1]:text-gh-fg [&_h1]:text-sm [&_h1]:font-semibold [&_h1]:mb-1
            [&_h2]:text-gh-fg [&_h2]:text-xs [&_h2]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1
            [&_h3]:text-gh-fg [&_h3]:text-xs [&_h3]:font-semibold [&_h3]:mt-2 [&_h3]:mb-0.5
            [&_table]:border-collapse [&_table]:w-full [&_table]:text-xs
            [&_th]:text-left [&_th]:font-medium [&_th]:text-gh-fg-muted [&_th]:pb-1 [&_th]:border-b [&_th]:border-gh-border [&_th]:pr-4
            [&_td]:py-1 [&_td]:pr-4 [&_td]:border-b [&_td]:border-gh-border/40
            [&_code]:bg-gh-canvas-subtle [&_code]:px-1 [&_code]:rounded [&_code]:text-xs
            [&_ul]:list-disc [&_ul]:pl-4 [&_li]:my-0.5
            [&_strong]:text-gh-fg">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{analysis.summary}</ReactMarkdown>
          </div>
        )}
        {(orchRecs.length > 0 || projRecs.length > 0) && (
          <div className="space-y-3">
            {orchRecs.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-gh-fg mb-1.5">Orchestrator Recommendations</div>
                <div className="space-y-1.5">
                  {orchRecs.map((rec, i) => (
                    <div key={i} className={`text-xs border rounded px-3 py-2 ${PRIORITY_COLORS[rec.priority] || 'text-gh-fg-muted border-gh-border bg-gh-canvas-subtle'}`}>
                      <div className="flex items-start gap-2">
                        <span className="uppercase font-semibold opacity-70 flex-shrink-0 text-[10px] mt-0.5">{rec.priority}</span>
                        <div>
                          <div>{rec.description}</div>
                          {rec.filePath && <div className="opacity-60 font-mono text-[10px] mt-0.5">{rec.filePath}</div>}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {projRecs.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-gh-fg mb-1.5">Project Recommendations</div>
                <div className="space-y-1.5">
                  {projRecs.map((rec, i) => (
                    <div key={i} className={`text-xs border rounded px-3 py-2 ${PRIORITY_COLORS[rec.priority] || 'text-gh-fg-muted border-gh-border bg-gh-canvas-subtle'}`}>
                      <div className="flex items-start gap-2">
                        <span className="uppercase font-semibold opacity-70 flex-shrink-0 text-[10px] mt-0.5">{rec.priority}</span>
                        <div>{rec.description}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function PipelineRunView() {
  const navigate = useNavigate()
  const searchParams = Route.useSearch()
  const urlRunId = searchParams.runId
  const contentTab = searchParams.contentTab

  const [activePipelineRuns, setActivePipelineRuns] = useState([])
  const [completedPipelineRuns, setCompletedPipelineRuns] = useState([])
  const [selectedPipelineRun, setSelectedPipelineRun] = useState(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [pipelineRunEvents, setPipelineRunEvents] = useState([])
  const [workflowConfig, setWorkflowConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingCompleted, setLoadingCompleted] = useState(false)
  const [loadingEvents, setLoadingEvents] = useState(false)
  const [completedLoadedCount, setCompletedLoadedCount] = useState(0)
  const [hasMoreCompleted, setHasMoreCompleted] = useState(true)
  const [analysis, setAnalysis] = useState(null)
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

  // Re-fetch completed runs when filters change (also handles initial mount).
  // Uses replace mode so the list stays visible until new data arrives,
  // avoiding a flash of "No runs match filters" during the fetch.
  useEffect(() => {
    fetchCompletedPipelineRuns(0, false, null, activeFilters, true)
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
    setSidebarOpen(false)
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

  // Fetch filter options from the dedicated aggregation endpoint (covers all projects/boards)
  const fetchFilterOptions = useCallback(async () => {
    try {
      const response = await fetch('/api/pipeline-run-filter-options')
      const data = await response.json()
      if (data.success) {
        setFilterOptions({
          projects: data.projects || [],
          boards: data.boards || [],
          outcomes: data.outcomes || [],
        })
      } else {
        console.warn('Filter options endpoint returned non-success:', data.error)
      }
    } catch (error) {
      console.error('Error fetching filter options:', error)
    }
  }, [])

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

  // Fetch completed pipeline runs — stable, ref-guarded.
  // replace=true atomically swaps the list with new data (used on filter change)
  // so the UI never shows an empty intermediate state.
  const fetchCompletedPipelineRuns = useCallback(async (offset = 0, append = false, customLimit = null, filters = null, replace = false) => {
    if (isFetchingCompletedRef.current && !replace) return

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
        setCompletedPipelineRuns(currentCompleted => {
          let mergedRuns
          if (replace) {
            // Atomic swap — keeps old list visible until this moment
            mergedRuns = data.runs
          } else if (append) {
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

        if (replace) {
          setCompletedLoadedCount(data.runs.length)
          setHasMoreCompleted(data.runs.length === limit)
        } else if (append) {
          setCompletedLoadedCount(prev => prev + data.runs.length)
          setHasMoreCompleted(data.runs.length === completedLimit)
        } else if (completedLoadedCountRef.current === 0 && data.runs.length > 0) {
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

  // Single filtered event array shared by all views — flow graph, event log, prompts.
  // Excludes claude_log/claude_api categories, infrastructure/telemetry types, and raw
  // Anthropic API streaming events. See shouldIncludePipelineEvent in EVENT_TYPE_MAP.js.
  // allEvents (unfiltered mergedEvents) is kept separately for token/tool enrichment.
  const pipelineEvents = useMemo(() =>
    mergedEvents.filter(shouldIncludePipelineEvent),
    [mergedEvents]
  )

  // Find latest agent execution ID — pipelineEvents is sorted ascending, so the last
  // agent_initialized event is the most recently started agent execution.
  const latestAgentExecutionId = useMemo(() => {
    if (!pipelineEvents || pipelineEvents.length === 0) return null
    const agentInitEvents = pipelineEvents.filter(event => event.event_type === 'agent_initialized')
    if (agentInitEvents.length === 0) return null
    const ev = agentInitEvents.at(-1)
    return ev.agent_execution_id || ev.data?.agent_execution_id || null
  }, [pipelineEvents])

  // Determine if currently in conversational loop
  const isConversational = useMemo(() => {
    if (!pipelineEvents || pipelineEvents.length === 0) return false
    const sortedEvents = [...pipelineEvents].sort((a, b) =>
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
  }, [pipelineEvents])

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
    fetchFilterOptions()
  }, [fetchActivePipelineRuns, fetchFilterOptions])

  // Periodic refresh every 10 seconds (filter options refresh every 60s)
  const filterRefreshCounterRef = useRef(0)
  useEffect(() => {
    const intervalId = setInterval(() => {
      fetchActivePipelineRuns(false)
      if (completedLoadedCountRef.current > 0) {
        const itemsToRefresh = Math.max(completedLoadedCountRef.current, completedLimit)
        fetchCompletedPipelineRuns(0, false, itemsToRefresh, activeFiltersRef.current)
      }
      filterRefreshCounterRef.current += 1
      if (filterRefreshCounterRef.current % 6 === 0) {
        fetchFilterOptions()
      }
    }, 10000)
    return () => clearInterval(intervalId)
  }, [fetchActivePipelineRuns, fetchCompletedPipelineRuns, fetchFilterOptions, completedLimit])

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

  // Fetch pipeline analysis (summary + recommendations) when run changes.
  // Clear immediately on every run switch so stale data from a previous run is
  // never shown while the new fetch is in flight or if the new run has no analysis.
  useEffect(() => {
    setAnalysis(null)
    if (!selectedRunId) return
    let cancelled = false
    fetch(`/api/pipeline-run/${selectedRunId}/analysis`)
      .then(r => r.json())
      .then(data => { if (!cancelled && data.success && data.analysis) setAnalysis(data.analysis) })
      .catch(err => console.error('[pipeline-run] analysis fetch error:', err))
    return () => { cancelled = true }
  }, [selectedRunId])

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
    <div className="min-h-screen flex flex-col p-2 md:p-5 bg-gh-canvas text-gh-fg">
      {!isFullscreen && <Header />}

      {!isFullscreen && (
        <div className="flex items-center justify-between my-3 flex-shrink-0">
          <NavigationTabs />
          <div className="flex items-center gap-2">
            <button
              onClick={() => setSidebarOpen(true)}
              className="md:hidden px-3 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
            >
              <List className="inline w-4 h-4 mr-1" />
              Runs
            </button>
            <button
              onClick={() => fetchActivePipelineRuns(true)}
              disabled={loading}
              className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
            >
              <RefreshCw className={`inline w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
              <span className="hidden md:inline">Refresh</span>
            </button>
          </div>
        </div>
      )}

      <div className={`flex gap-0 md:gap-4 ${isFullscreen ? 'fixed inset-0 z-50 p-5 bg-gh-canvas' : 'flex-1 min-h-0'}`}>
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
          isOpen={sidebarOpen}
          onToggle={() => setSidebarOpen(prev => !prev)}
        />

        {/* Main Content Area */}
        <div className="relative bg-gh-canvas-subtle border border-gh-border p-2 md:p-4 flex flex-col flex-1 min-w-0 min-h-0 rounded-md overflow-hidden">
          {selectedPipelineRun ? (
            <>
              <PipelineRunHeader
                pipelineRun={selectedPipelineRun}
                analysis={analysis}
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
                    contentTab === 'graph' || (contentTab === 'report' && !analysis)
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
                <button
                  onClick={() => updateUrlParams({ contentTab: 'prompts' }, true)}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    contentTab === 'prompts'
                      ? 'border-gh-accent-emphasis text-gh-accent-fg'
                      : 'border-transparent text-gh-fg-muted hover:text-gh-fg hover:border-gh-border-muted'
                  }`}
                >
                  Prompts
                </button>
                {analysis && (
                  <button
                    onClick={() => updateUrlParams({ contentTab: 'report' }, true)}
                    className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                      contentTab === 'report'
                        ? 'border-gh-accent-emphasis text-gh-accent-fg'
                        : 'border-transparent text-gh-fg-muted hover:text-gh-fg hover:border-gh-border-muted'
                    }`}
                  >
                    Report
                  </button>
                )}
              </div>

              {/* Content Area */}
              <div className="flex-1 min-h-[300px] md:min-h-0 flex">
                {contentTab === 'report' && analysis ? (
                  <PipelineAnalysisReport analysis={analysis} />
                ) : contentTab === 'prompts' ? (
                  <div className="flex-1 min-h-0">
                    <PromptsFlowGraph
                      events={pipelineEvents}
                      selectedPipelineRun={selectedPipelineRun}
                      loading={loadingEvents}
                    />
                  </div>
                ) : contentTab === 'log' ? (
                  <div className="flex-1 overflow-auto">
                    {loadingEvents ? (
                      <div className="flex items-center justify-center h-32 text-gh-fg-muted text-sm">
                        Loading events…
                      </div>
                    ) : (
                      <PipelineRunEventLog
                        pipelineRun={selectedPipelineRun}
                        events={pipelineEvents}
                      />
                    )}
                  </div>
                ) : (
                  <div className="flex-1 min-h-0">
                    <PipelineFlowGraph
                      graphEvents={pipelineEvents}
                      allEvents={mergedEvents}
                      workflowConfig={workflowConfig}
                      selectedPipelineRun={selectedPipelineRun}
                      minZoom={0.5}
                      maxZoom={1.5}
                      height="100%"
                      loading={loadingEvents}
                      emptyMessage="No events found for this pipeline run"
                      fitViewAlign={selectedPipelineRun.status === 'active' ? 'active-node' : 'top'}
                    />
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
      contentTab: search.contentTab === 'log'     ? 'log'
              : search.contentTab === 'report'  ? 'report'
              : search.contentTab === 'prompts' ? 'prompts'
              : 'graph',
    }
  },
})
