import { createFileRoute } from '@tanstack/react-router'
import { RefreshCw, Activity, CheckCircle } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import PipelineRunEventLog from '../components/PipelineRunEventLog'
import { useState, useEffect, useCallback, useMemo, useRef, useLayoutEffect } from 'react'
import { useSocket } from '../contexts/SocketContext'
import { formatDuration } from '../utils/stateHelpers'
import { mergePipelineRunEvents } from '../utils/eventMerging'

function PipelineRunDebugView() {
  const [activePipelineRuns, setActivePipelineRuns] = useState([])
  const [completedPipelineRuns, setCompletedPipelineRuns] = useState([])
  const [selectedPipelineRun, setSelectedPipelineRun] = useState(null)
  const [pipelineRunEvents, setPipelineRunEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadingCompleted, setLoadingCompleted] = useState(false)
  const [loadingEvents, setLoadingEvents] = useState(false)
  const [selectedTab, setSelectedTab] = useState('active')
  const [completedOffset, setCompletedOffset] = useState(0)
  const [hasMoreCompleted, setHasMoreCompleted] = useState(true)
  const completedLimit = 10
  const { events: socketEvents } = useSocket()

  // Use refs to track current values without causing re-renders
  const selectedPipelineRunRef = useRef(selectedPipelineRun)
  const selectedTabRef = useRef(selectedTab)
  const isFetchingActiveRef = useRef(false)
  const isFetchingCompletedRef = useRef(false)

  // Refs for scroll position preservation
  const activeListScrollRef = useRef(null)
  const completedListScrollRef = useRef(null)
  const savedActiveScrollPos = useRef(0)
  const savedCompletedScrollPos = useRef(0)

  // Update refs when state changes
  useEffect(() => {
    selectedPipelineRunRef.current = selectedPipelineRun
  }, [selectedPipelineRun])

  useEffect(() => {
    selectedTabRef.current = selectedTab
  }, [selectedTab])

  // Continuously save scroll position to handle updates
  useEffect(() => {
    const activeEl = activeListScrollRef.current
    const completedEl = completedListScrollRef.current

    const handleActiveScroll = () => {
      if (activeEl) {
        savedActiveScrollPos.current = activeEl.scrollTop
      }
    }

    const handleCompletedScroll = () => {
      if (completedEl) {
        savedCompletedScrollPos.current = completedEl.scrollTop
      }
    }

    if (activeEl) {
      activeEl.addEventListener('scroll', handleActiveScroll, { passive: true })
    }
    if (completedEl) {
      completedEl.addEventListener('scroll', handleCompletedScroll, { passive: true })
    }

    return () => {
      if (activeEl) {
        activeEl.removeEventListener('scroll', handleActiveScroll)
      }
      if (completedEl) {
        completedEl.removeEventListener('scroll', handleCompletedScroll)
      }
    }
  }, [selectedTab])

  // Restore scroll position after render (before paint)
  useLayoutEffect(() => {
    if (activeListScrollRef.current && savedActiveScrollPos.current > 0) {
      activeListScrollRef.current.scrollTop = savedActiveScrollPos.current
    }
  }, [activePipelineRuns])

  useLayoutEffect(() => {
    if (completedListScrollRef.current && savedCompletedScrollPos.current > 0) {
      completedListScrollRef.current.scrollTop = savedCompletedScrollPos.current
    }
  }, [completedPipelineRuns])

  // Fetch active pipeline runs - no dependencies to avoid circular re-creation
  const fetchActivePipelineRuns = useCallback(async () => {
    // Guard against concurrent fetches
    if (isFetchingActiveRef.current) {
      console.log('[PipelineRunDebug] Skipping active fetch - already in progress')
      return
    }

    try {
      isFetchingActiveRef.current = true
      setLoading(true)
      const response = await fetch('/active-pipeline-runs')
      const data = await response.json()

      if (data.success) {
        console.log('[PipelineRunDebug] Fetched active pipeline runs:', data.runs)
        setActivePipelineRuns(data.runs)

        // Update selectedPipelineRun if it exists in the refreshed data
        // Use functional update to avoid dependency
        setSelectedPipelineRun(currentSelected => {
          if (currentSelected) {
            const updatedRun = data.runs.find(run => run.id === currentSelected.id)
            if (updatedRun) {
              console.log('[PipelineRunDebug] Updating selected pipeline run with fresh data')
              return updatedRun
            }
            return currentSelected
          } else if (data.runs.length > 0) {
            // Auto-select first run if none selected
            return data.runs[0]
          }
          return currentSelected
        })
      }
    } catch (error) {
      console.error('Error fetching active pipeline runs:', error)
    } finally {
      setLoading(false)
      isFetchingActiveRef.current = false
    }
  }, [])
  
  // Fetch completed pipeline runs with pagination - no dependencies to avoid circular re-creation
  const fetchCompletedPipelineRuns = useCallback(async (offset = 0, append = false) => {
    // Guard against concurrent fetches
    if (isFetchingCompletedRef.current) {
      console.log('[PipelineRunDebug] Skipping completed fetch - already in progress')
      return
    }

    try {
      isFetchingCompletedRef.current = true
      setLoadingCompleted(true)
      const response = await fetch(`/completed-pipeline-runs?limit=${completedLimit}&offset=${offset}`)
      const data = await response.json()

      if (data.success) {
        console.log('[PipelineRunDebug] Fetched completed pipeline runs:', data.runs.length)

        // Use functional update to avoid dependency on completedPipelineRuns
        setCompletedPipelineRuns(currentCompleted => {
          const allCompletedRuns = append ? [...currentCompleted, ...data.runs] : data.runs

          // Update selectedPipelineRun if it exists in the completed runs
          // Use ref to get current value without dependency
          setSelectedPipelineRun(currentSelected => {
            if (currentSelected) {
              const updatedRun = allCompletedRuns.find(run => run.id === currentSelected.id)
              if (updatedRun) {
                console.log('[PipelineRunDebug] Updating selected pipeline run from completed list')
                return updatedRun
              }
              return currentSelected
            } else if (data.runs.length > 0 && selectedTabRef.current === 'completed') {
              // Auto-select first completed run if on completed tab and none selected
              return data.runs[0]
            }
            return currentSelected
          })

          return allCompletedRuns
        })

        setHasMoreCompleted(data.runs.length === completedLimit)
      }
    } catch (error) {
      console.error('Error fetching completed pipeline runs:', error)
    } finally {
      setLoadingCompleted(false)
      isFetchingCompletedRef.current = false
    }
  }, [completedLimit])
  
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
  
  // Initial load
  useEffect(() => {
    fetchActivePipelineRuns()
    fetchCompletedPipelineRuns(0, false)
  }, [])

  // Periodic refresh to keep data up-to-date
  useEffect(() => {
    const intervalId = setInterval(() => {
      console.log('[PipelineRunDebug] Periodic refresh')
      fetchActivePipelineRuns()
      // Use ref to check current tab without adding dependency
      if (selectedTabRef.current === 'completed') {
        fetchCompletedPipelineRuns(0, false)
      }
    }, 10000) // Refresh every 10 seconds

    return () => clearInterval(intervalId)
    // Callbacks are now stable (no dependencies), so only depend on them
  }, [fetchActivePipelineRuns, fetchCompletedPipelineRuns])
  
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
  
  // Track last processed socket event to prevent duplicate processing
  const lastProcessedEventRef = useRef(null)

  // Update on socket events
  useEffect(() => {
    if (socketEvents.length > 0) {
      const latestEvent = socketEvents[socketEvents.length - 1]

      // Skip if we've already processed this event
      if (lastProcessedEventRef.current === latestEvent) {
        return
      }
      lastProcessedEventRef.current = latestEvent

      // Refresh pipeline runs and events when new events arrive
      if (['agent_initialized', 'agent_completed', 'agent_failed'].includes(latestEvent.event_type)) {
        // Use ref to get current value without dependency
        if (selectedPipelineRunRef.current) {
          fetchPipelineRunEvents(selectedPipelineRunRef.current.id)
        }
        // Refresh both active and completed runs to catch status transitions
        fetchActivePipelineRuns()
        fetchCompletedPipelineRuns(0, false)
      }
    }
  }, [socketEvents, fetchPipelineRunEvents, fetchActivePipelineRuns, fetchCompletedPipelineRuns])
  
  // Merge API events with live WebSocket events
  const mergedEvents = useMemo(() => {
    if (!selectedPipelineRun) return []
    return mergePipelineRunEvents(pipelineRunEvents, socketEvents, selectedPipelineRun)
  }, [pipelineRunEvents, socketEvents, selectedPipelineRun])

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
        {/* Pipeline Run Selector Sidebar */}
        <div className="w-64 bg-gh-canvas-subtle rounded-md border border-gh-border p-4 h-fit">
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
                <div
                  ref={activeListScrollRef}
                  className="space-y-2 max-h-[70vh] overflow-y-auto overscroll-contain"
                  style={{ scrollBehavior: 'auto' }}
                >
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
                  <div
                    ref={completedListScrollRef}
                    className="space-y-2 max-h-[70vh] overflow-y-auto overscroll-contain"
                    style={{ scrollBehavior: 'auto' }}
                  >
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
        
        {/* Pipeline Run Event Log */}
        <div className="flex-1">
          {loadingEvents ? (
            <div className="flex items-center justify-center h-96 bg-gh-canvas-subtle rounded-md border border-gh-border">
              <RefreshCw className="w-8 h-8 animate-spin text-gh-accent-primary" />
            </div>
          ) : (
            <PipelineRunEventLog
              pipelineRun={selectedPipelineRun}
              events={mergedEvents}
              isActive={selectedPipelineRun?.status === 'running'}
            />
          )}
        </div>
      </div>
    </div>
  )
}

export const Route = createFileRoute('/pipeline-run-debug')({
  component: PipelineRunDebugView,
})
