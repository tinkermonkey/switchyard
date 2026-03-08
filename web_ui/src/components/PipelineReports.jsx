import { useState, useEffect, useCallback } from 'react'
import { ChevronUp, ChevronDown, Info } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Modal from './Modal'

const PAGE_SIZE = 20

const PRIORITY_COLORS = {
  high: 'text-red-600 border-red-700/40 bg-white/20',
  medium: 'text-yellow-600 border-yellow-700/40 bg-white/20',
  low: 'text-blue-600 border-blue-700/40 bg-white/20',
}

const SORTABLE_COLS = ['project', 'issue_number', 'board', 'outcome', 'started_at']

function OutcomeBadge({ outcome }) {
  if (outcome === 'success') {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded border text-xs font-medium bg-gh-success/10 text-gh-success border-gh-success/30">
        success
      </span>
    )
  }
  if (outcome === 'failed') {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded border text-xs font-medium bg-gh-danger/10 text-gh-danger border-gh-danger/30">
        failed
      </span>
    )
  }
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded border text-xs font-medium bg-gh-canvas-subtle text-gh-fg-muted border-gh-border">
      {outcome || 'unknown'}
    </span>
  )
}

function PriorityPills({ recs }) {
  if (!recs || recs.length === 0) return <span className="text-gh-fg-muted">-</span>

  const counts = { high: 0, medium: 0, low: 0 }
  for (const rec of recs) {
    const p = rec.priority?.toLowerCase()
    if (p in counts) counts[p]++
  }

  return (
    <span className="flex items-center gap-1 text-xs">
      <span className="text-gh-fg-muted">{recs.length}</span>
      {counts.high > 0 && <span className="text-red-500 font-medium">H:{counts.high}</span>}
      {counts.medium > 0 && <span className="text-yellow-500 font-medium">M:{counts.medium}</span>}
      {counts.low > 0 && <span className="text-blue-500 font-medium">L:{counts.low}</span>}
    </span>
  )
}

function PipelineAnalysisReport({ analysis }) {
  const orchRecs = analysis.orchestratorRecommendations || []
  const projRecs = analysis.projectRecommendations || []
  return (
    <div className="space-y-5">
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
  )
}

function SortHeader({ col, label, sortCol, sortDir, onSort }) {
  const isActive = sortCol === col
  return (
    <button
      className="flex items-center gap-1 hover:text-gh-fg transition-colors"
      onClick={() => onSort(col)}
    >
      {label}
      {isActive ? (
        sortDir === 'asc'
          ? <ChevronUp className="w-3 h-3" />
          : <ChevronDown className="w-3 h-3" />
      ) : (
        <ChevronDown className="w-3 h-3 opacity-30" />
      )}
    </button>
  )
}

function Pagination({ page, total, pageSize, onPageChange }) {
  const totalPages = Math.ceil(total / pageSize)
  if (totalPages <= 1) return null

  const start = page * pageSize + 1
  const end = Math.min((page + 1) * pageSize, total)

  // Build visible page numbers (up to 5, with ellipsis for large sets)
  const getPageNumbers = () => {
    if (totalPages <= 5) return Array.from({ length: totalPages }, (_, i) => i)
    const pages = []
    if (page <= 2) {
      pages.push(0, 1, 2, 3, '...', totalPages - 1)
    } else if (page >= totalPages - 3) {
      pages.push(0, '...', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1)
    } else {
      pages.push(0, '...', page - 1, page, page + 1, '...', totalPages - 1)
    }
    return pages
  }

  return (
    <div className="flex items-center justify-between mt-4 text-sm text-gh-fg-muted">
      <span>Showing {start}–{end} of {total} runs</span>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onPageChange(page - 1)}
          disabled={page === 0}
          className="px-2 py-1 rounded border border-gh-border bg-gh-canvas-subtle hover:bg-gh-border-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors text-xs"
        >
          Prev
        </button>
        {getPageNumbers().map((p, i) =>
          p === '...'
            ? <span key={`ellipsis-${i}`} className="px-1">…</span>
            : (
              <button
                key={p}
                onClick={() => onPageChange(p)}
                className={`px-2 py-1 rounded border text-xs transition-colors ${
                  p === page
                    ? 'border-gh-accent-primary bg-gh-accent-emphasis text-white'
                    : 'border-gh-border bg-gh-canvas-subtle hover:bg-gh-border-muted'
                }`}
              >
                {p + 1}
              </button>
            )
        )}
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages - 1}
          className="px-2 py-1 rounded border border-gh-border bg-gh-canvas-subtle hover:bg-gh-border-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors text-xs"
        >
          Next
        </button>
      </div>
    </div>
  )
}

export default function PipelineReports({ search, onSearchChange }) {
  const { project, board, outcome, page, sortCol, sortDir } = search

  const [runs, setRuns] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filterOptions, setFilterOptions] = useState({ projects: [], boards: [], outcomes: [] })
  const [modalRun, setModalRun] = useState(null)
  const [modalAnalysis, setModalAnalysis] = useState(null)
  const [modalLoading, setModalLoading] = useState(false)

  const fetchRuns = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      if (project) params.set('project', project)
      if (board) params.set('board', board)
      if (outcome) params.set('outcome', outcome)
      params.set('sort_col', sortCol)
      params.set('sort_dir', sortDir)
      const res = await fetch(`/completed-pipeline-runs?${params}`)
      const data = await res.json()
      if (data.success) {
        setRuns(data.runs || [])
        setTotal(data.total ?? data.count ?? data.runs?.length ?? 0)
        setFilterOptions({
          projects: [...new Set(data.runs.map(r => r.project).filter(Boolean))].sort(),
          boards: [...new Set(data.runs.map(r => r.board).filter(Boolean))].sort(),
          outcomes: [...new Set([
            ...data.runs.map(r => r.outcome).filter(Boolean),
            ...(data.runs.some(r => !r.outcome) ? ['unknown'] : []),
          ])].sort(),
        })
      } else {
        setError(data.error || 'Failed to load pipeline runs')
        setRuns([])
      }
    } catch (err) {
      console.error('[PipelineReports] fetch error:', err)
      setError('Network error — could not load pipeline runs')
      setRuns([])
    } finally {
      setLoading(false)
    }
  }, [project, board, outcome, page, sortCol, sortDir])

  useEffect(() => { fetchRuns() }, [fetchRuns])

  const handleFilterChange = (key, value) => {
    onSearchChange({ [key]: value, page: 0 })
  }

  const handleSort = (col) => {
    if (!SORTABLE_COLS.includes(col)) return
    if (col === sortCol) {
      onSearchChange({ sortDir: sortDir === 'asc' ? 'desc' : 'asc', page: 0 })
    } else {
      onSearchChange({ sortCol: col, sortDir: 'desc', page: 0 })
    }
  }

  const handlePageChange = (newPage) => {
    onSearchChange({ page: newPage })
  }

  const openModal = async (run) => {
    setModalRun(run)
    setModalAnalysis(null)
    setModalLoading(true)
    try {
      const res = await fetch(`/api/pipeline-run/${run.id}/analysis`)
      const data = await res.json()
      if (data.success && data.analysis) setModalAnalysis(data.analysis)
    } catch (err) {
      console.error('[PipelineReports] analysis fetch error:', err)
    } finally {
      setModalLoading(false)
    }
  }

  const closeModal = () => {
    setModalRun(null)
    setModalAnalysis(null)
  }

  const headerCell = "px-3 py-2 text-left text-xs font-medium text-gh-fg-muted uppercase tracking-wide"
  const bodyCell = "px-3 py-2 text-sm text-gh-fg align-middle"

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gh-fg">Pipeline Reports</h2>
      </div>

      {/* Filter bar */}
      <div className="flex gap-3 mb-4 flex-wrap">
        <select
          value={project}
          onChange={(e) => handleFilterChange('project', e.target.value)}
          className="px-3 py-1.5 bg-gh-canvas-subtle border border-gh-border rounded text-sm text-gh-fg focus:outline-none focus:border-gh-accent-primary"
        >
          <option value="">All Projects</option>
          {filterOptions.projects.map(p => <option key={p} value={p}>{p}</option>)}
        </select>
        <select
          value={board}
          onChange={(e) => handleFilterChange('board', e.target.value)}
          className="px-3 py-1.5 bg-gh-canvas-subtle border border-gh-border rounded text-sm text-gh-fg focus:outline-none focus:border-gh-accent-primary"
        >
          <option value="">All Boards</option>
          {filterOptions.boards.map(b => <option key={b} value={b}>{b}</option>)}
        </select>
        <select
          value={outcome}
          onChange={(e) => handleFilterChange('outcome', e.target.value)}
          className="px-3 py-1.5 bg-gh-canvas-subtle border border-gh-border rounded text-sm text-gh-fg focus:outline-none focus:border-gh-accent-primary"
        >
          <option value="">All Outcomes</option>
          {filterOptions.outcomes.map(o => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>

      {/* Table */}
      <div className="w-full border border-gh-border rounded-md overflow-hidden">
        <table className="w-full border-collapse">
          <thead>
            <tr className="bg-gh-canvas-subtle">
              <th className={headerCell}>
                <SortHeader col="project" label="Project" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
              </th>
              <th className={headerCell}>
                <SortHeader col="issue_number" label="Issue" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
              </th>
              <th className={headerCell}>
                <SortHeader col="board" label="Board" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
              </th>
              <th className={headerCell}>
                <SortHeader col="outcome" label="Success" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
              </th>
              <th className={`${headerCell} text-center`}>Orch. Recs</th>
              <th className={`${headerCell} text-center`}>Project Recs</th>
              <th className={`${headerCell} text-center`}>Info</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-gh-fg-muted text-sm">
                  Loading…
                </td>
              </tr>
            ) : error ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-gh-danger text-sm">
                  {error}
                </td>
              </tr>
            ) : runs.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-gh-fg-muted text-sm">
                  No pipeline runs found
                </td>
              </tr>
            ) : runs.map((run) => (
              <tr key={run.id} className="border-t border-gh-border hover:bg-gh-canvas-subtle transition-colors">
                <td className={bodyCell}>
                  <span className="text-xs font-mono">{run.project || '—'}</span>
                </td>
                <td className={bodyCell}>
                  {run.issue_number ? (
                    <div className="flex flex-col">
                      <a
                        href={run.issue_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-gh-accent-fg hover:underline text-xs font-medium"
                      >
                        #{run.issue_number}
                      </a>
                      {run.issue_title && (
                        <span className="text-xs text-gh-fg-muted truncate max-w-xs" title={run.issue_title}>
                          {run.issue_title.length > 60 ? run.issue_title.slice(0, 60) + '…' : run.issue_title}
                        </span>
                      )}
                    </div>
                  ) : '—'}
                </td>
                <td className={bodyCell}>
                  <span className="text-xs">{run.board || '—'}</span>
                </td>
                <td className={bodyCell}>
                  <OutcomeBadge outcome={run.outcome} />
                </td>
                <td className={`${bodyCell} text-center`}>
                  <PriorityPills recs={run.orchestratorRecommendations} />
                </td>
                <td className={`${bodyCell} text-center`}>
                  <PriorityPills recs={run.projectRecommendations} />
                </td>
                <td className={`${bodyCell} text-center`}>
                  <button
                    onClick={() => openModal(run)}
                    className="text-gh-fg-muted hover:text-gh-fg transition-colors"
                    title="View analysis report"
                  >
                    <Info className="w-4 h-4" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Pagination
        page={page}
        total={total}
        pageSize={PAGE_SIZE}
        onPageChange={handlePageChange}
      />

      {/* Report modal */}
      {modalRun && (
        <Modal
          title={`${modalRun.issue_number ? `#${modalRun.issue_number}` : 'Pipeline Run'}${modalRun.issue_title ? ` — ${modalRun.issue_title}` : ''}`}
          onClose={closeModal}
        >
          {modalLoading ? (
            <div className="flex items-center justify-center h-32 text-gh-fg-muted text-sm">
              Loading analysis…
            </div>
          ) : modalAnalysis ? (
            <PipelineAnalysisReport analysis={modalAnalysis} />
          ) : (
            <div className="flex items-center justify-center h-32 text-gh-fg-muted text-sm">
              No analysis available for this run.
            </div>
          )}
        </Modal>
      )}
    </div>
  )
}
