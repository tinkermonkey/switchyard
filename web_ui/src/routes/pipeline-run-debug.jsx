import { createFileRoute, Link } from '@tanstack/react-router'
import { RefreshCw, Activity, CheckCircle, ArrowRight, XCircle } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import PipelineRunEventLog from '../components/PipelineRunEventLog'
import ConfirmationModal from '../components/ConfirmationModal'
import { useState, useEffect, useCallback, useMemo, useRef, useLayoutEffect, memo } from 'react'
import { useSocket } from '../contexts/SocketContext'
import { formatDuration } from '../utils/stateHelpers'
import { mergePipelineRunEvents, mergeArrayByIdStable } from '../utils/eventMerging'

// Memoized component for pipeline run list item
// Custom comparison function that only compares run object and isSelected
const PipelineRunItem = memo(({ run, isSelected, onClick }) => {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left p-3 rounded border transition-colors ${
        isSelected
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
  )
}, (prevProps, nextProps) => {
  // Custom comparison: only re-render if run object changed (by reference) or isSelected changed
  // Ignore onClick changes since it's recreated but functionally the same
  return prevProps.run === nextProps.run && prevProps.isSelected === nextProps.isSelected
})

PipelineRunItem.displayName = 'PipelineRunItem'

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
  const [showKillModal, setShowKillModal] = useState(false)
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
  const fetchActivePipelineRuns = useCallback(async (isInitialLoad = false) => {
    // Guard against concurrent fetches
    if (isFetchingActiveRef.current) {
      console.log('[PipelineRunDebug] Skipping active fetch - already in progress')
      return
    }

    try {
      isFetchingActiveRef.current = true
      // Only show loading spinner on initial load, not on background refreshes
      if (isInitialLoad) {
        setLoading(true)
      }
      const response = await fetch('/active-pipeline-runs')
      const data = await response.json()

      if (data.success) {
        //console.log('[PipelineRunDebug] Fetched active pipeline runs:', data.runs)

        // Use stable merge to preserve object references when data hasn't changed
        setActivePipelineRuns(currentRuns => {
          const mergedRuns = mergeArrayByIdStable(currentRuns, data.runs)

          // Update selectedPipelineRun if it exists in the refreshed data
          setSelectedPipelineRun(currentSelected => {
            if (currentSelected) {
              const updatedRun = mergedRuns.find(run => run.id === currentSelected.id)
              if (updatedRun) {
                //console.log('[PipelineRunDebug] Updating selected pipeline run with fresh data')
                return updatedRun
              }
              return currentSelected
            } else if (mergedRuns.length > 0) {
              // Auto-select first run if none selected
              return mergedRuns[0]
            }
            return currentSelected
          })

          return mergedRuns
        })
      }
    } catch (error) {
      console.error('Error fetching active pipeline runs:', error)
    } finally {
      if (isInitialLoad) {
        setLoading(false)
      }
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
        //console.log('[PipelineRunDebug] Fetched completed pipeline runs:', data.runs.length)

        // Use functional update to avoid dependency on completedPipelineRuns
        setCompletedPipelineRuns(currentCompleted => {
          // Append new runs if loading more, or replace if refreshing
          const combinedRuns = append ? [...currentCompleted, ...data.runs] : data.runs

          // Use stable merge to preserve object references
          const mergedRuns = mergeArrayByIdStable(
            append ? currentCompleted : [],
            combinedRuns
          )

          // Update selectedPipelineRun if it exists in the completed runs
          setSelectedPipelineRun(currentSelected => {
            if (currentSelected) {
              const updatedRun = mergedRuns.find(run => run.id === currentSelected.id)
              if (updatedRun) {
                //console.log('[PipelineRunDebug] Updating selected pipeline run from completed list')
                return updatedRun
              }
              return currentSelected
            } else if (data.runs.length > 0 && selectedTabRef.current === 'completed') {
              // Auto-select first completed run if on completed tab and none selected
              return mergedRuns[0]
            }
            return currentSelected
          })

          return mergedRuns
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
        // Use stable merge for events too (events might have an 'id' or use index)
        setPipelineRunEvents(currentEvents => {
          // For events, we'll use timestamp as a pseudo-ID since they might not have IDs
          const newEventsWithKeys = data.events.map((event, idx) => ({
            ...event,
            _key: event.id || `${event.timestamp}_${idx}`
          }))
          const currentEventsWithKeys = currentEvents.map((event, idx) => ({
            ...event,
            _key: event.id || event._key || `${event.timestamp}_${idx}`
          }))

          return mergeArrayByIdStable(currentEventsWithKeys, newEventsWithKeys, '_key')
        })
      }
    } catch (error) {
      console.error('Error fetching pipeline run events:', error)
    } finally {
      setLoadingEvents(false)
    }
  }, [])
  
  // Initial load
  useEffect(() => {
    fetchActivePipelineRuns(true) // Pass true for initial load
    fetchCompletedPipelineRuns(0, false)
  }, [])

  // Periodic refresh to keep data up-to-date
  useEffect(() => {
    const intervalId = setInterval(() => {
      //console.log('[PipelineRunDebug] Periodic refresh')
      fetchActivePipelineRuns(false) // Background refresh, no loading spinner
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
  
  // Track last processed socket event timestamp to prevent duplicate processing
  const lastProcessedTimestampRef = useRef(0)

  // Update on socket events
  useEffect(() => {
    if (socketEvents.length > 0) {
      // Get the NEWEST event (first in array since new events are prepended)
      const latestEvent = socketEvents[0]

      // Extract timestamp - handle both Unix timestamp and ISO string
      let eventTimestamp = 0
      if (latestEvent.timestamp) {
        if (typeof latestEvent.timestamp === 'number') {
          eventTimestamp = latestEvent.timestamp > 10000000000
            ? latestEvent.timestamp / 1000
            : latestEvent.timestamp
        } else if (typeof latestEvent.timestamp === 'string') {
          const date = new Date(latestEvent.timestamp)
          if (!isNaN(date.getTime())) {
            eventTimestamp = date.getTime() / 1000
          }
        }
      }

      // Skip if we've already processed this timestamp
      if (eventTimestamp <= lastProcessedTimestampRef.current) {
        return
      }

      console.log('[PipelineRunDebug] Processing new socket event:', {
        event_type: latestEvent.event_type,
        timestamp: eventTimestamp,
        lastProcessed: lastProcessedTimestampRef.current
      })

      lastProcessedTimestampRef.current = eventTimestamp

      // Refresh pipeline runs and events when relevant events arrive
      // Expand the list of event types we care about for pipeline runs
      const pipelineEventTypes = [
        'agent_initialized',
        'agent_completed',
        'agent_failed',
        'pipeline_stage_started',
        'pipeline_stage_completed',
        'review_cycle_started',
        'review_cycle_completed'
      ]

      if (pipelineEventTypes.includes(latestEvent.event_type)) {
        console.log('[PipelineRunDebug] Refreshing pipeline data for event type:', latestEvent.event_type)

        // Use ref to get current value without dependency
        if (selectedPipelineRunRef.current) {
          fetchPipelineRunEvents(selectedPipelineRunRef.current.id)
        }
        // Refresh both active and completed runs to catch status transitions
        fetchActivePipelineRuns(false) // Background refresh from WebSocket event
        fetchCompletedPipelineRuns(0, false)
      }
    }
  }, [socketEvents, fetchPipelineRunEvents, fetchActivePipelineRuns, fetchCompletedPipelineRuns])
  
  // Merge API events with live WebSocket events
  const mergedEvents = useMemo(() => {
    if (!selectedPipelineRun) return []
    const allEvents = mergePipelineRunEvents(pipelineRunEvents, socketEvents, selectedPipelineRun)

    console.log('[PipelineRunDebug] All merged events:', {
      count: allEvents.length,
      sampleEvents: allEvents.slice(0, 3).map(e => ({
        event_type: e.event_type,
        event_category: e.event_category,
        timestamp: e.timestamp
      }))
    })

    // Exclude only Claude streaming logs, show everything else
    const filtered = allEvents.filter(event => {
      // Exclude Claude API streaming events by category
      if (event.event_category === 'claude_api') return false

      // Exclude Claude streaming events by type
      const claudeStreamEventTypes = [
        'claude_stream',
        'claude_stream_event',
        'text_output',
        'text_delta',
        'tool_call',
        'tool_use',
        'tool_result',
        'input_json_delta',
        'message_start',
        'message_delta',
        'message_stop',
        'content_block_start',
        'content_block_delta',
        'content_block_stop'
      ]

      if (claudeStreamEventTypes.includes(event.event_type)) {
        return false
      }

      // Also check raw_event structure for nested Claude streaming data
      if (event.raw_event) {
        const rawEventType = event.raw_event?.event?.type || event.raw_event?.type
        if (claudeStreamEventTypes.includes(rawEventType)) {
          return false
        }
      }

      // Include everything else
      return true
    })

    console.log('[PipelineRunDebug] Filtered events (excluding Claude logs):', {
      count: filtered.length,
      excludedCount: allEvents.length - filtered.length,
      sampleEvents: filtered.slice(0, 3).map(e => ({
        event_type: e.event_type,
        event_category: e.event_category
      }))
    })

    return filtered
  }, [pipelineRunEvents, socketEvents, selectedPipelineRun])

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

  // Find the most recent agent execution for the selected pipeline run
  const latestAgentExecutionId = useMemo(() => {
    if (!mergedEvents || mergedEvents.length === 0) return null

    // Look for agent_initialized events (most recent first since events are sorted)
    const agentInitEvents = mergedEvents.filter(event => event.event_type === 'agent_initialized')

    if (agentInitEvents.length === 0) return null

    // Get the most recent agent execution ID
    const latestEvent = agentInitEvents[0]
    return latestEvent.agent_execution_id || null
  }, [mergedEvents])

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      
      <div className="flex items-center justify-between my-3">
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
                    <PipelineRunItem
                      key={run.id}
                      run={run}
                      isSelected={selectedPipelineRun?.id === run.id}
                      onClick={() => setSelectedPipelineRun(run)}
                    />
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
                      <PipelineRunItem
                        key={run.id}
                        run={run}
                        isSelected={selectedPipelineRun?.id === run.id}
                        onClick={() => setSelectedPipelineRun(run)}
                      />
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
          {/* Actions Bar */}
          {selectedPipelineRun && (
            <div className="mb-3 flex gap-2">
              {latestAgentExecutionId && (
                <Link
                  to="/agent-execution/$executionId"
                  params={{ executionId: latestAgentExecutionId }}
                  className="inline-flex items-center gap-2 px-4 py-2 bg-gh-canvas border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
                >
                  <span>View Latest Agent Execution</span>
                  <ArrowRight className="w-4 h-4" />
                </Link>
              )}
              
              {selectedPipelineRun.status === 'active' && (
                <button
                  onClick={handleKillRun}
                  className="px-3 py-2 text-sm bg-red-900/20 border border-red-800 text-red-400 rounded hover:bg-red-900/40 transition-colors whitespace-nowrap flex items-center gap-1"
                  title="Kill this pipeline run"
                >
                  <XCircle className="w-4 h-4" />
                  Kill Run
                </button>
              )}
            </div>
          )}

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

export const Route = createFileRoute('/pipeline-run-debug')({
  component: PipelineRunDebugView,
})
