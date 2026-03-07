import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, TrendingUp } from 'lucide-react'

const DAYS_OPTIONS = [7, 14, 30]

const fmt = (n) => {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(Math.round(n))
}

const fmtDur = (ms) => {
  if (!ms) return '—'
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)}m`
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)}s`
  return `${Math.round(ms)}ms`
}

function StatCell({ label, value, sub }) {
  return (
    <div className="flex flex-col">
      <span className="text-xs text-gh-fg-muted">{label}</span>
      <span className="font-mono text-sm font-semibold">{value}</span>
      {sub && <span className="text-xs text-gh-fg-muted font-mono">{sub}</span>}
    </div>
  )
}

function SectionHeader({ title }) {
  return (
    <h4 className="text-xs font-semibold text-gh-fg-muted uppercase tracking-wide mb-2">{title}</h4>
  )
}

function ProjectCard({ project: p }) {
  const outcomes = p.pipeline_outcomes || {}
  const tokens = p.tokens || {}
  const context = p.context || {}
  const tools = p.tool_calls || {}
  const rv = p.review_cycles || {}
  const rp = p.repair_cycles || {}
  const pr = p.pr_review_cycles || {}

  const successRate = outcomes.success_rate
  const successBadge = successRate != null
    ? successRate >= 80
      ? 'bg-gh-success-subtle border-gh-success text-gh-success'
      : successRate >= 50
        ? 'bg-gh-attention-subtle border-gh-attention text-gh-attention'
        : 'bg-gh-danger-subtle border-gh-danger text-gh-danger'
    : 'bg-gh-canvas-subtle border-gh-border text-gh-fg-muted'

  return (
    <div className="border border-gh-border rounded-md overflow-hidden">
      {/* Card header */}
      <div className="px-4 py-3 border-b border-gh-border bg-gh-canvas-subtle flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-semibold font-mono">{p.project}</h3>
          <span className="text-xs text-gh-fg-muted">{p.pipeline_run_count} pipeline runs</span>
          {p.days_with_data > 1 && (
            <span className="text-xs text-gh-fg-muted">({p.days_with_data}d with data)</span>
          )}
        </div>
        {successRate != null && (
          <span className={`px-2 py-0.5 border rounded text-xs font-semibold ${successBadge}`}>
            {successRate}% success
          </span>
        )}
      </div>

      <div className="p-4 space-y-5">
        {/* Tokens */}
        <div>
          <SectionHeader title="Tokens" />
          <div className="bg-gh-canvas-subtle border border-gh-border rounded p-3 grid grid-cols-2 sm:grid-cols-4 gap-3">
            <StatCell
              label="Direct Input"
              value={fmt(tokens.sum_direct_input)}
              sub={`avg ${fmt(tokens.avg_direct_input_per_run)}/run`}
            />
            <StatCell
              label="Cache Reads"
              value={fmt(tokens.sum_cache_read)}
              sub={`avg ${fmt(tokens.avg_cache_read_per_run)}/run`}
            />
            <StatCell
              label="Cache Creation"
              value={fmt(tokens.sum_cache_creation)}
              sub={`avg ${fmt(tokens.avg_cache_creation_per_run)}/run`}
            />
            <StatCell
              label="Output"
              value={fmt(tokens.sum_output)}
              sub={`avg ${fmt(tokens.avg_output_per_run)}/run`}
            />
          </div>
        </div>

        {/* Context + Tools */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <SectionHeader title="Context" />
            <div className="bg-gh-canvas-subtle border border-gh-border rounded p-3 grid grid-cols-2 gap-3">
              <StatCell
                label="Peak Context"
                value={fmt(context.peak_max_context)}
              />
              <StatCell
                label="Avg Max Context/Run"
                value={fmt(context.avg_max_context_per_run)}
              />
              <StatCell
                label="Total Initial"
                value={fmt(context.sum_initial_input)}
                sub={`avg ${fmt(context.avg_initial_input_per_run)}/run`}
              />
            </div>
          </div>
          <div>
            <SectionHeader title="Tool Calls" />
            <div className="bg-gh-canvas-subtle border border-gh-border rounded p-3 grid grid-cols-2 gap-3">
              <StatCell
                label="Total Invocations"
                value={fmt(tools.total_invocations)}
              />
              <StatCell
                label="Avg/Run"
                value={fmt(tools.avg_invocations_per_run)}
              />
            </div>

            <div className="mt-4">
              <SectionHeader title="Pipeline Outcomes" />
            </div>
            <div className="bg-gh-canvas-subtle border border-gh-border rounded p-3 grid grid-cols-2 gap-3 mt-2">
              <StatCell label="Success" value={outcomes.success_count ?? '—'} />
              <StatCell label="Failed" value={outcomes.failed_count ?? '—'} />
            </div>
          </div>
        </div>

        {/* Cycle summaries */}
        <div>
          <SectionHeader title="Cycles" />
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {/* Review cycles */}
            <div className="bg-gh-canvas-subtle border border-gh-border rounded p-3">
              <p className="text-xs font-semibold text-gh-fg mb-2">Review Cycles</p>
              {rv.total_count > 0 ? (
                <div className="space-y-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Total</span>
                    <span className="font-mono font-semibold">{rv.total_count}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Avg iterations</span>
                    <span className="font-mono">{rv.avg_iterations}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Max iterations</span>
                    <span className="font-mono">{rv.max_iterations}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Escalations</span>
                    <span className={`font-mono ${rv.escalation_count > 0 ? 'text-gh-attention' : ''}`}>
                      {rv.escalation_count}
                    </span>
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gh-fg-muted">No data</p>
              )}
            </div>

            {/* Repair cycles */}
            <div className="bg-gh-canvas-subtle border border-gh-border rounded p-3">
              <p className="text-xs font-semibold text-gh-fg mb-2">Repair Cycles</p>
              {rp.total_count > 0 ? (
                <div className="space-y-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Total</span>
                    <span className="font-mono font-semibold">{rp.total_count}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Avg test cycles</span>
                    <span className="font-mono">{rp.avg_test_cycles}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Max test cycles</span>
                    <span className="font-mono">{rp.max_test_cycles}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Avg fix cycles</span>
                    <span className="font-mono">{rp.avg_fix_cycles}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Avg test duration</span>
                    <span className="font-mono">{fmtDur(rp.avg_test_duration_ms)}</span>
                  </div>
                  {rp.systemic_analysis_count > 0 && (
                    <div className="flex justify-between">
                      <span className="text-gh-fg-muted">Systemic analyses</span>
                      <span className="font-mono text-gh-attention">{rp.systemic_analysis_count}</span>
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-xs text-gh-fg-muted">No data</p>
              )}
            </div>

            {/* PR review cycles */}
            <div className="bg-gh-canvas-subtle border border-gh-border rounded p-3">
              <p className="text-xs font-semibold text-gh-fg mb-2">PR Review Cycles</p>
              {pr.total_count > 0 ? (
                <div className="space-y-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Total</span>
                    <span className="font-mono font-semibold">{pr.total_count}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Avg iterations</span>
                    <span className="font-mono">{pr.avg_iterations}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gh-fg-muted">Max iterations</span>
                    <span className="font-mono">{pr.max_iterations}</span>
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gh-fg-muted">No data</p>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function ProjectMetrics({ days, onDaysChange }) {
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const fetchMetrics = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/metrics/projects?days=${days}`)
      const data = await res.json()
      if (data.success) {
        setProjects(data.projects || [])
      } else {
        setError(data.error || 'Failed to load project metrics')
      }
    } catch {
      setError('Failed to load project metrics')
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => {
    fetchMetrics()
  }, [fetchMetrics])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <TrendingUp className="w-5 h-5 text-gh-accent-primary" />
          <h2 className="text-lg font-semibold">Project Metrics</h2>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex gap-1">
            {DAYS_OPTIONS.map(d => (
              <button
                key={d}
                onClick={() => onDaysChange(d)}
                className={`px-3 py-1 text-sm rounded border transition-colors ${
                  days === d
                    ? 'bg-gh-accent-emphasis border-gh-accent-primary text-white'
                    : 'bg-gh-canvas-subtle border-gh-border hover:bg-gh-border-muted'
                }`}
              >
                {d}d
              </button>
            ))}
          </div>
          <button
            onClick={fetchMetrics}
            disabled={loading}
            className="p-1.5 bg-gh-canvas-subtle border border-gh-border rounded hover:bg-gh-border-muted transition-colors disabled:opacity-50"
            title="Refresh"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {error && (
        <div className="p-3 bg-gh-danger-subtle border border-gh-danger rounded text-sm text-gh-danger">
          {error}
        </div>
      )}

      {loading && (
        <div className="flex items-center justify-center py-12">
          <RefreshCw className="w-6 h-6 animate-spin text-gh-accent-primary" />
        </div>
      )}

      {!loading && projects.length === 0 && (
        <div className="p-8 bg-gh-canvas-subtle border border-gh-border rounded-md text-center">
          <TrendingUp className="w-8 h-8 text-gh-fg-muted mx-auto mb-3" />
          <p className="text-gh-fg-muted text-sm font-medium mb-1">No project metrics yet</p>
          <p className="text-gh-fg-muted text-xs">
            Project metrics are computed by a daily scheduled job.
            Check back after the job has run at least once, or trigger a backfill manually.
          </p>
        </div>
      )}

      {!loading && projects.length > 0 && (
        <div className="grid grid-cols-1 gap-4">
          {projects.map(p => (
            <ProjectCard key={p.project} project={p} />
          ))}
        </div>
      )}
    </div>
  )
}
