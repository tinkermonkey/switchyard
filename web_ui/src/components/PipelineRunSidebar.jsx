import { memo, useRef, useLayoutEffect, useEffect, useMemo } from 'react'
import { RefreshCw, Activity, CheckCircle } from 'lucide-react'
import { formatDuration, formatRunDuration } from '../utils/stateHelpers'

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
          Duration: {formatRunDuration(run.duration)}
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
  onSelectRun,
  onLoadMore,
  activeFilters,
  onFiltersChange,
  filterOptions,
  isOpen = false,
  onToggle,
}) {
  const completedListScrollRef = useRef(null)
  const savedCompletedScrollPos = useRef(0)

  const { project: projectFilter, board: boardFilter, outcome: outcomeFilter } = activeFilters
  const { projects: projectOptions, boards: boardOptions, outcomes: outcomeOptions } = filterOptions

  // Save completed scroll position continuously
  useEffect(() => {
    const el = completedListScrollRef.current
    if (!el) return
    const handleScroll = () => { savedCompletedScrollPos.current = el.scrollTop }
    el.addEventListener('scroll', handleScroll, { passive: true })
    return () => el.removeEventListener('scroll', handleScroll)
  }, [])

  // Restore completed scroll position after render
  useLayoutEffect(() => {
    const el = completedListScrollRef.current
    const savedPos = savedCompletedScrollPos.current
    if (el && savedPos >= 0) {
      el.scrollTop = savedPos
      requestAnimationFrame(() => {
        if (el && el.scrollTop !== savedPos) el.scrollTop = savedPos
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

  const hasActiveFilters = projectFilter || boardFilter || outcomeFilter

  const sidebarContent = (
    <>
      <div className="flex items-center gap-2 mb-3 flex-shrink-0">
        <h3 className="text-lg font-semibold">Pipeline Runs</h3>
      </div>

      {/* Active section */}
      <div className="flex-shrink-0">
        <div className="flex items-center gap-2 mb-2">
          <Activity className="w-4 h-4 text-gh-fg-muted" />
          <span className="text-sm font-medium">Active</span>
          {activePipelineRuns.length > 0 && (
            <span className="ml-auto text-xs bg-gh-accent-emphasis text-white rounded-full px-1.5 py-0.5 leading-none">
              {activePipelineRuns.length}
            </span>
          )}
        </div>

        {loading ? (
          <p className="text-gh-fg-muted text-xs px-1 mb-2">Loading...</p>
        ) : activePipelineRuns.length === 0 ? (
          <p className="text-gh-fg-muted text-xs px-1 mb-2">No active runs</p>
        ) : (
          <div className="space-y-2 mb-2">
            {activeRunsList}
          </div>
        )}
      </div>

      <div className="border-t border-gh-border my-2 flex-shrink-0" />

      {/* Completed section */}
      <div className="flex flex-col flex-1 min-h-0">
        <div className="flex items-center gap-2 mb-2 flex-shrink-0">
          <CheckCircle className="w-4 h-4 text-gh-fg-muted" />
          <span className="text-sm font-medium">Completed</span>
          {completedPipelineRuns.length > 0 && (
            <span className="ml-auto text-xs bg-gh-canvas border border-gh-border rounded-full px-1.5 py-0.5 leading-none text-gh-fg-muted">
              {completedPipelineRuns.length}
            </span>
          )}
        </div>

        {/* Filters */}
        {(projectOptions.length > 1 || boardOptions.length > 1 || outcomeOptions.length > 0) && (
          <div className="flex flex-col gap-1 mb-2 flex-shrink-0">
            {projectOptions.length > 1 && (
              <select
                value={projectFilter}
                onChange={e => onFiltersChange({ ...activeFilters, project: e.target.value })}
                className="w-full text-xs bg-gh-canvas border border-gh-border rounded px-2 py-2 md:py-1 text-gh-fg"
              >
                <option value="">Project</option>
                {projectOptions.map(p => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            )}
            {boardOptions.length > 1 && (
              <select
                value={boardFilter}
                onChange={e => onFiltersChange({ ...activeFilters, board: e.target.value })}
                className="w-full text-xs bg-gh-canvas border border-gh-border rounded px-2 py-2 md:py-1 text-gh-fg"
              >
                <option value="">Board</option>
                {boardOptions.map(b => (
                  <option key={b} value={b}>{b}</option>
                ))}
              </select>
            )}
            {outcomeOptions.length > 0 && (
              <select
                value={outcomeFilter}
                onChange={e => onFiltersChange({ ...activeFilters, outcome: e.target.value })}
                className="w-full text-xs bg-gh-canvas border border-gh-border rounded px-2 py-2 md:py-1 text-gh-fg"
              >
                <option value="">Outcome</option>
                {outcomeOptions.map(o => (
                  <option key={o} value={o}>{o.charAt(0).toUpperCase() + o.slice(1)}</option>
                ))}
              </select>
            )}
          </div>
        )}

        {loadingCompleted && completedPipelineRuns.length === 0 ? (
          <p className="text-gh-fg-muted text-xs px-1">Loading...</p>
        ) : completedPipelineRuns.length === 0 ? (
          <p className="text-gh-fg-muted text-xs px-1">
            {hasActiveFilters ? 'No runs match filters' : 'No completed runs'}
          </p>
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
    </>
  )

  return (
    <>
      {/* Desktop: static sidebar */}
      <div className="hidden md:flex w-64 flex-shrink-0 bg-gh-canvas-subtle rounded-md border border-gh-border p-4 flex-col min-h-0">
        {sidebarContent}
      </div>

      {/* Mobile: bottom sheet */}
      <div className="md:hidden">
        {/* Backdrop */}
        {isOpen && (
          <div
            className="fixed inset-0 bg-black/50 z-30"
            onClick={onToggle}
          />
        )}

        {/* Sheet */}
        <div
          className={`fixed inset-x-0 bottom-0 z-40 bg-gh-canvas-subtle border-t border-gh-border rounded-t-xl max-h-[70vh] flex flex-col p-4 transform transition-transform duration-200 ease-out ${
            isOpen ? 'translate-y-0' : 'translate-y-full'
          }`}
        >
          {/* Drag handle */}
          <div className="flex justify-center mb-3 flex-shrink-0">
            <div className="w-10 h-1 bg-gh-border-muted rounded-full" />
          </div>
          {sidebarContent}
        </div>
      </div>
    </>
  )
}
