import { useState, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Modal from './Modal'

const PRIORITY_COLORS = {
  high: 'text-red-600 border-red-700/40 bg-white/20',
  medium: 'text-yellow-600 border-yellow-700/40 bg-white/20',
  low: 'text-blue-600 border-blue-700/40 bg-white/20',
}

const PRIORITY_ORDER = ['high', 'medium', 'low']

const PRIORITY_LABEL_COLORS = {
  high: 'text-red-500',
  medium: 'text-yellow-500',
  low: 'text-blue-500',
}

const PRIORITY_BORDER = {
  high: 'border-l-red-500',
  medium: 'border-l-yellow-500',
  low: 'border-l-blue-500',
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

function RecItem({ rec, onClick }) {
  const priority = rec.priority?.toLowerCase() || 'low'
  const borderColor = PRIORITY_BORDER[priority] || 'border-l-gh-border'
  const run = rec.run || {}

  return (
    <div
      className={`px-4 py-3 border-b border-gh-border/40 hover:bg-gh-canvas-subtle cursor-pointer border-l-2 ${borderColor}`}
      onClick={onClick}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-2 min-w-0">
          <span className={`uppercase font-semibold text-[10px] mt-0.5 flex-shrink-0 ${PRIORITY_LABEL_COLORS[priority] || 'text-gh-fg-muted'}`}>
            [{priority.toUpperCase()}]
          </span>
          <div className="min-w-0">
            <div className="text-sm text-gh-fg">{rec.description}</div>
            {rec.filePath && (
              <div className="text-[10px] font-mono text-gh-fg-muted mt-0.5 truncate">{rec.filePath}</div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0 text-xs">
          <span className="px-1.5 py-0.5 rounded border border-gh-border bg-gh-canvas-subtle text-gh-fg-muted text-[10px]">
            {rec.rec_type}
          </span>
          {run.issue_number && (
            <a
              href={run.issue_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-gh-accent-fg hover:underline truncate max-w-[180px]"
              title={run.issue_title}
              onClick={(e) => e.stopPropagation()}
            >
              #{run.issue_number}{run.issue_title ? ` ${run.issue_title.length > 40 ? run.issue_title.slice(0, 40) + '…' : run.issue_title}` : ''}
            </a>
          )}
        </div>
      </div>
    </div>
  )
}

function ProjectCard({ project, recs, onRecClick }) {
  const byPriority = {}
  for (const rec of recs) {
    const p = rec.priority?.toLowerCase() || 'low'
    if (!byPriority[p]) byPriority[p] = []
    byPriority[p].push(rec)
  }

  return (
    <div className="border border-gh-border rounded-md overflow-hidden mb-4">
      <div className="px-4 py-3 bg-gh-canvas-subtle border-b border-gh-border flex justify-between items-center">
        <span className="font-semibold text-gh-fg text-sm font-mono">{project}</span>
        <span className="text-xs text-gh-fg-muted">{recs.length} rec{recs.length !== 1 ? 's' : ''}</span>
      </div>
      {PRIORITY_ORDER.filter(p => byPriority[p]?.length > 0).map(priority => (
        <div key={priority}>
          <div className="px-4 py-2 text-xs font-semibold uppercase text-gh-fg-muted border-b border-gh-border/50 bg-gh-canvas">
            {priority} ({byPriority[priority].length})
          </div>
          {byPriority[priority].map((rec, i) => (
            <RecItem key={i} rec={rec} onClick={() => onRecClick(rec)} />
          ))}
        </div>
      ))}
    </div>
  )
}

export default function RecommendationReports({ search, onSearchChange }) {
  const { project, recType } = search

  const [recommendations, setRecommendations] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filterOptions, setFilterOptions] = useState({ projects: [] })
  const [modalRun, setModalRun] = useState(null)
  const [modalAnalysis, setModalAnalysis] = useState(null)
  const [modalLoading, setModalLoading] = useState(false)

  useEffect(() => {
    fetch('/api/pipeline-run-filter-options')
      .then(r => r.json())
      .then(data => {
        if (data.success) setFilterOptions({ projects: data.projects || [] })
      })
      .catch(err => console.error('[RecommendationReports] filter options error:', err))
  }, [])

  const fetchRecommendations = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (project) params.set('project', project)
      if (recType && recType !== 'all') params.set('rec_type', recType)
      const res = await fetch(`/api/pipeline-recommendations?${params}`)
      const data = await res.json()
      if (data.success) {
        setRecommendations(data.recommendations || [])
      } else {
        setError(data.error || 'Failed to load recommendations')
        setRecommendations([])
      }
    } catch (err) {
      console.error('[RecommendationReports] fetch error:', err)
      setError('Network error — could not load recommendations')
      setRecommendations([])
    } finally {
      setLoading(false)
    }
  }, [project, recType])

  useEffect(() => { fetchRecommendations() }, [fetchRecommendations])

  const openModal = async (rec) => {
    const run = rec.run
    if (!run?.id) return
    setModalRun(run)
    setModalAnalysis(null)
    setModalLoading(true)
    try {
      const res = await fetch(`/api/pipeline-run/${run.id}/analysis`)
      const data = await res.json()
      if (data.success && data.analysis) setModalAnalysis(data.analysis)
    } catch (err) {
      console.error('[RecommendationReports] analysis fetch error:', err)
    } finally {
      setModalLoading(false)
    }
  }

  const closeModal = () => {
    setModalRun(null)
    setModalAnalysis(null)
  }

  // Group by project
  const byProject = {}
  for (const rec of recommendations) {
    const p = rec.project || '(unknown)'
    if (!byProject[p]) byProject[p] = []
    byProject[p].push(rec)
  }
  const projectGroups = Object.entries(byProject).sort(([a], [b]) => a.localeCompare(b))

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gh-fg">Recommendation Reports</h2>
      </div>

      {/* Filter bar */}
      <div className="flex gap-3 mb-4 flex-wrap">
        <select
          value={project}
          onChange={(e) => onSearchChange({ project: e.target.value })}
          className="px-3 py-1.5 bg-gh-canvas-subtle border border-gh-border rounded text-sm text-gh-fg focus:outline-none focus:border-gh-accent-primary"
        >
          <option value="">All Projects</option>
          {filterOptions.projects.map(p => <option key={p} value={p}>{p}</option>)}
        </select>
        <select
          value={recType}
          onChange={(e) => onSearchChange({ recType: e.target.value })}
          className="px-3 py-1.5 bg-gh-canvas-subtle border border-gh-border rounded text-sm text-gh-fg focus:outline-none focus:border-gh-accent-primary"
        >
          <option value="all">All Types</option>
          <option value="orchestrator">Orchestrator</option>
          <option value="project">Project</option>
        </select>
      </div>

      {/* Content */}
      {loading ? (
        <div className="py-12 text-center text-gh-fg-muted text-sm">Loading…</div>
      ) : error ? (
        <div className="py-12 text-center text-gh-danger text-sm">{error}</div>
      ) : projectGroups.length === 0 ? (
        <div className="py-12 text-center text-gh-fg-muted text-sm">No recommendations found</div>
      ) : (
        projectGroups.map(([proj, recs]) => (
          <ProjectCard key={proj} project={proj} recs={recs} onRecClick={openModal} />
        ))
      )}

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
