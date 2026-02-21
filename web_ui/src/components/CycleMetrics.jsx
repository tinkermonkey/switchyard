import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, BarChart2 } from 'lucide-react'

const DAYS_OPTIONS = [1, 3, 7]  // server clamps cycle metrics to 7 days max

const CYCLE_COLORS = {
  review_cycle: { bg: 'bg-blue-900/30', border: 'border-blue-600', text: 'text-blue-400', label: 'Review Cycle' },
  repair_cycle: { bg: 'bg-orange-900/30', border: 'border-orange-600', text: 'text-orange-400', label: 'Repair Cycle' },
  pr_review_stage: { bg: 'bg-purple-900/30', border: 'border-purple-600', text: 'text-purple-400', label: 'PR Review Stage' },
}

const DEFAULT_COLOR = { bg: 'bg-gh-canvas-subtle', border: 'border-gh-border', text: 'text-gh-fg', label: null }

const formatTokenCount = (n) => {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(Math.round(n))
}

function TokenRow({ label, avg, min, max }) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-gh-border last:border-0">
      <span className="text-xs text-gh-fg-muted">{label}</span>
      <div className="text-right">
        <span className="font-mono text-sm font-semibold">{formatTokenCount(avg)}</span>
        {(min != null && max != null) && (
          <span className="text-xs text-gh-fg-muted font-mono ml-2">
            ({formatTokenCount(min)} – {formatTokenCount(max)})
          </span>
        )}
      </div>
    </div>
  )
}

function CycleCard({ cycle }) {
  const colors = CYCLE_COLORS[cycle.cycle_type] || DEFAULT_COLOR
  const label = colors.label || cycle.cycle_type
  const tc = cycle.task_count || 0

  const agentBreakdown = Object.entries(cycle.agent_breakdown || {})
    .sort((a, b) => (b[1].task_count || 0) - (a[1].task_count || 0))

  const toolBreakdown = Object.entries(cycle.tool_breakdown || {})
    .sort((a, b) => (b[1].sum_context_growth || 0) - (a[1].sum_context_growth || 0))
    .slice(0, 10)

  const models = Object.keys(cycle.model_breakdown || {})

  return (
    <div className={`${colors.bg} border ${colors.border} rounded-md overflow-hidden`}>
      <div className={`px-4 py-3 border-b ${colors.border}`}>
        <div className="flex items-center justify-between">
          <h3 className={`text-sm font-semibold ${colors.text}`}>{label}</h3>
          <span className="text-xs text-gh-fg-muted">{tc} executions</span>
        </div>
      </div>

      <div className="p-4 space-y-4">
        {/* Four-type token summary */}
        <div className="bg-gh-canvas-subtle rounded p-3 border border-gh-border">
          <TokenRow
            label="Avg Direct Input"
            avg={cycle.avg_direct_input}
          />
          <TokenRow
            label="Avg Cache Reads"
            avg={cycle.avg_cache_read}
          />
          <TokenRow
            label="Avg Cache Creation"
            avg={cycle.avg_cache_creation}
          />
          <TokenRow
            label="Avg Output"
            avg={cycle.avg_output}
            min={cycle.min_output}
            max={cycle.max_output}
          />
          <TokenRow
            label="Avg Max Context"
            avg={cycle.avg_max_context}
            min={cycle.min_max_context}
            max={cycle.max_max_context}
          />
          <TokenRow
            label="Avg Initial Context"
            avg={cycle.avg_initial_input}
          />
        </div>

        {/* 2-col grid: agent breakdown + models */}
        <div className="grid grid-cols-2 gap-4">
          {/* Agent breakdown */}
          <div>
            <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Agent Breakdown</h4>
            {agentBreakdown.length > 0 ? (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gh-fg-muted">
                    <th className="text-left font-normal pb-1">Agent</th>
                    <th className="text-right font-normal pb-1">Runs</th>
                    <th className="text-right font-normal pb-1">Avg Direct</th>
                    <th className="text-right font-normal pb-1">Avg Cache R</th>
                    <th className="text-right font-normal pb-1">Avg Output</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gh-border">
                  {agentBreakdown.map(([agent, stats]) => (
                    <tr key={agent}>
                      <td className="font-mono py-0.5">{agent}</td>
                      <td className="text-right py-0.5">{stats.task_count || 0}</td>
                      <td className="text-right py-0.5 font-mono">{formatTokenCount(stats.avg_direct_input)}</td>
                      <td className="text-right py-0.5 font-mono">{formatTokenCount(stats.avg_cache_read)}</td>
                      <td className="text-right py-0.5 font-mono">{formatTokenCount(stats.avg_output)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="text-gh-fg-muted text-xs">No agent data</p>
            )}
          </div>

          {/* Models */}
          <div>
            {models.length > 0 && (
              <div>
                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Models</h4>
                <div className="flex flex-wrap gap-1">
                  {models.map(m => (
                    <span key={m} className="px-1.5 py-0.5 bg-gh-accent-subtle border border-gh-accent-muted rounded text-xs font-mono">
                      {m.split('-').slice(-2).join('-')}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Tool Breakdown */}
        {toolBreakdown.length > 0 && (
          <div>
            <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tool Breakdown (top by context growth)</h4>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gh-fg-muted">
                  <th className="text-left font-normal pb-1">Tool</th>
                  <th className="text-right font-normal pb-1">Invocations</th>
                  <th className="text-right font-normal pb-1">Avg Output/Call</th>
                  <th className="text-right font-normal pb-1">Context Growth</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gh-border">
                {toolBreakdown.map(([tool, tb]) => {
                  const inv = tb.task_count || 0
                  return (
                    <tr key={tool}>
                      <td className="font-mono py-0.5">{tool}</td>
                      <td className="text-right py-0.5">{inv}</td>
                      <td className="text-right py-0.5 font-mono">
                        {inv > 0 ? formatTokenCount((tb.sum_output || 0) / inv) : '—'}
                      </td>
                      <td className="text-right py-0.5 font-mono">{formatTokenCount(tb.sum_context_growth)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

export default function CycleMetrics({ days, onDaysChange }) {
  const [metrics, setMetrics] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const fetchMetrics = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/metrics/cycles?days=${days}`)
      const data = await res.json()
      if (data.success) {
        setMetrics(data.metrics || [])
      } else {
        setError(data.error || 'Failed to load cycle metrics')
      }
    } catch (err) {
      setError('Failed to load cycle metrics')
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
          <BarChart2 className="w-5 h-5 text-gh-accent-primary" />
          <h2 className="text-lg font-semibold">Cycle Token Metrics</h2>
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

      {!loading && metrics.length === 0 && (
        <div className="p-8 bg-gh-canvas-subtle border border-gh-border rounded-md text-center">
          <BarChart2 className="w-8 h-8 text-gh-fg-muted mx-auto mb-3" />
          <p className="text-gh-fg-muted text-sm font-medium mb-1">No metrics data yet</p>
          <p className="text-gh-fg-muted text-xs">
            Token metrics are computed by a scheduled job (every 3 hours by default).
            Check back after the job has run at least once.
          </p>
        </div>
      )}

      {!loading && metrics.length > 0 && (
        <div className="grid grid-cols-1 gap-4">
          {metrics.map(cycle => (
            <CycleCard key={cycle.cycle_type} cycle={cycle} />
          ))}
        </div>
      )}
    </div>
  )
}
