import { memo, useRef, useLayoutEffect, useEffect, useMemo } from 'react'
import { RefreshCw, Activity, CheckCircle } from 'lucide-react'
import { formatDuration } from '../utils/stateHelpers'

// Memoized run list item — only re-renders when run object or selection changes
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
  return prevProps.run === nextProps.run && prevProps.isSelected === nextProps.isSelected
})

PipelineRunItem.displayName = 'PipelineRunItem'

export default function PipelineRunSidebar({
  activePipelineRuns,
  completedPipelineRuns,
  selectedPipelineRun,
  loading,
  loadingCompleted,
  hasMoreCompleted,
  selectedTab,
  onSelectRun,
  onTabChange,
  onLoadMore,
}) {
  const activeListScrollRef = useRef(null)
  const completedListScrollRef = useRef(null)
  const savedActiveScrollPos = useRef(0)
  const savedCompletedScrollPos = useRef(0)

  // Continuously save scroll position to handle updates
  useEffect(() => {
    const activeEl = activeListScrollRef.current
    const completedEl = completedListScrollRef.current

    const handleActiveScroll = () => {
      if (activeEl) savedActiveScrollPos.current = activeEl.scrollTop
    }
    const handleCompletedScroll = () => {
      if (completedEl) savedCompletedScrollPos.current = completedEl.scrollTop
    }

    if (activeEl) activeEl.addEventListener('scroll', handleActiveScroll, { passive: true })
    if (completedEl) completedEl.addEventListener('scroll', handleCompletedScroll, { passive: true })

    return () => {
      if (activeEl) activeEl.removeEventListener('scroll', handleActiveScroll)
      if (completedEl) completedEl.removeEventListener('scroll', handleCompletedScroll)
    }
  }, [selectedTab])

  // Restore scroll position after render (before paint)
  useLayoutEffect(() => {
    const scrollEl = activeListScrollRef.current
    const savedPos = savedActiveScrollPos.current
    if (scrollEl && savedPos >= 0) {
      scrollEl.scrollTop = savedPos
      requestAnimationFrame(() => {
        if (scrollEl && scrollEl.scrollTop !== savedPos) scrollEl.scrollTop = savedPos
      })
    }
  }, [activePipelineRuns])

  useLayoutEffect(() => {
    const scrollEl = completedListScrollRef.current
    const savedPos = savedCompletedScrollPos.current
    if (scrollEl && savedPos >= 0) {
      scrollEl.scrollTop = savedPos
      requestAnimationFrame(() => {
        if (scrollEl && scrollEl.scrollTop !== savedPos) scrollEl.scrollTop = savedPos
      })
    }
  }, [completedPipelineRuns])

  const activeRunsList = useMemo(() => {
    return activePipelineRuns.map(run => (
      <PipelineRunItem
        key={run.id}
        run={run}
        isSelected={selectedPipelineRun?.id === run.id}
        onClick={() => onSelectRun(run)}
      />
    ))
  }, [activePipelineRuns, selectedPipelineRun, onSelectRun])

  const completedRunsList = useMemo(() => {
    return completedPipelineRuns.map(run => (
      <PipelineRunItem
        key={run.id}
        run={run}
        isSelected={selectedPipelineRun?.id === run.id}
        onClick={() => onSelectRun(run)}
      />
    ))
  }, [completedPipelineRuns, selectedPipelineRun, onSelectRun])

  return (
    <div className="w-64 flex-shrink-0 bg-gh-canvas-subtle rounded-md border border-gh-border p-4 flex flex-col min-h-0">
      <h3 className="text-lg font-semibold mb-3 flex-shrink-0">Pipeline Runs</h3>

      {/* Active / Completed tab switcher */}
      <div className="flex gap-2 mb-4 border-b border-gh-border flex-shrink-0">
        <button
          onClick={() => onTabChange('active')}
          className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
            selectedTab === 'active'
              ? 'border-gh-accent-emphasis text-gh-accent-fg'
              : 'border-transparent text-gh-fg-muted hover:text-gh-fg hover:border-gh-border-muted'
          }`}
        >
          Active
        </button>
        <button
          onClick={() => onTabChange('completed')}
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
        <div className="flex flex-col flex-1 min-h-0">
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
              className="space-y-2 overflow-y-auto overscroll-contain flex-1"
              style={{ scrollBehavior: 'auto' }}
            >
              {activeRunsList}
            </div>
          )}
        </div>
      )}

      {/* Completed Pipeline Runs */}
      {selectedTab === 'completed' && (
        <div className="flex flex-col flex-1 min-h-0">
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
                className="space-y-2 overflow-y-auto overscroll-contain flex-1"
                style={{ scrollBehavior: 'auto' }}
              >
                {completedRunsList}
              </div>

              {hasMoreCompleted && (
                <button
                  onClick={onLoadMore}
                  disabled={loadingCompleted}
                  className="w-full mt-3 px-4 py-2 bg-gh-canvas border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm disabled:opacity-50 flex-shrink-0"
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
        </div>
      )}
    </div>
  )
}
