import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, BarChart2 } from 'lucide-react'

const DAYS_OPTIONS = [1, 7, 30]

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
  return String(n)
}

function StatBox({ label, avg, min, max }) {
  return (
    <div className="bg-gh-canvas-subtle rounded p-2 text-center border border-gh-border">
      <div className="text-xs text-gh-fg-muted mb-1">{label}</div>
      <div className="font-mono text-sm font-semibold">{formatTokenCount(avg)}</div>
      {(min != null && max != null) && (
        <div className="text-xs text-gh-fg-muted font-mono mt-0.5">
          {formatTokenCount(min)} – {formatTokenCount(max)}
        </div>
      )}
    </div>
  )
}

function CycleCard({ cycle }) {
  const colors = CYCLE_COLORS[cycle.cycle_type] || DEFAULT_COLOR
  const label = colors.label || cycle.cycle_type

  const agentBreakdown = Object.entries(cycle.agent_breakdown || {})
    .sort((a, b) => (b[1].sample_count || 0) - (a[1].sample_count || 0))

  const topTools = Object.entries(cycle.tool_call_counts || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)

  const toolAttribution = Object.entries(cycle.tool_token_attribution || {})
    .sort((a, b) => b[1].total_tokens - a[1].total_tokens)

  const models = Object.keys(cycle.model_breakdown || {})

  return (
    <div className={`${colors.bg} border ${colors.border} rounded-md overflow-hidden`}>
      <div className={`px-4 py-3 border-b ${colors.border}`}>
        <div className="flex items-center justify-between">
          <h3 className={`text-sm font-semibold ${colors.text}`}>{label}</h3>
          <span className="text-xs text-gh-fg-muted">{cycle.sample_count} executions</span>
        </div>
      </div>

      <div className="p-4 space-y-4">
        {/* 6-stat token summary: 3 columns × 2 rows */}
        <div className="grid grid-cols-3 gap-3">
          <StatBox
            label="Avg Input"
            avg={cycle.avg_total_input}
            min={cycle.min_total_input}
            max={cycle.max_total_input}
          />
          <StatBox
            label="Avg Output"
            avg={cycle.avg_total_output}
            min={cycle.min_total_output}
            max={cycle.max_total_output}
          />
          <StatBox
            label="Avg Total"
            avg={cycle.avg_total_all}
            min={cycle.min_total_all}
            max={cycle.max_total_all}
          />
          <StatBox label="Min Total" avg={cycle.min_total_all} />
          <StatBox label="Max Total" avg={cycle.max_total_all} />
          <StatBox label="Avg Initial" avg={cycle.avg_initial_input} />
        </div>

        {/* 2-col grid: agent breakdown + top tools & models */}
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
                    <th className="text-right font-normal pb-1">Avg Input</th>
                    <th className="text-right font-normal pb-1">Avg Output</th>
                    <th className="text-right font-normal pb-1">Avg Total</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gh-border">
                  {agentBreakdown.map(([agent, stats]) => (
                    <tr key={agent}>
                      <td className="font-mono py-0.5">{agent}</td>
                      <td className="text-right py-0.5">{stats.sample_count || 0}</td>
                      <td className="text-right py-0.5 font-mono">{formatTokenCount(stats.avg_total_input)}</td>
                      <td className="text-right py-0.5 font-mono">{formatTokenCount(stats.avg_total_output)}</td>
                      <td className="text-right py-0.5 font-mono">{formatTokenCount(stats.avg_total_all)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="text-gh-fg-muted text-xs">No agent data</p>
            )}
          </div>

          {/* Top tools + models */}
          <div className="space-y-3">
            {topTools.length > 0 && (
              <div>
                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Top Tools</h4>
                <div className="flex flex-wrap gap-1">
                  {topTools.map(([name, count]) => (
                    <span key={name} className="text-xs text-gh-fg-muted font-mono">
                      {name}×{count}
                    </span>
                  ))}
                </div>
              </div>
            )}
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

        {/* Tool Token Attribution */}
        {toolAttribution.length > 0 && (
          <div>
            <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tool Token Attribution</h4>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gh-fg-muted">
                  <th className="text-left font-normal pb-1">Tool</th>
                  <th className="text-right font-normal pb-1">Calls</th>
                  <th className="text-right font-normal pb-1">Avg Tokens/Call</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gh-border">
                {toolAttribution.map(([tool, attr]) => (
                  <tr key={tool}>
                    <td className="font-mono py-0.5">{tool}</td>
                    <td className="text-right py-0.5">{attr.call_count}</td>
                    <td className="text-right py-0.5 font-mono">{formatTokenCount(attr.avg_tokens_per_call)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

export default function CycleMetrics() {
  const [days, setDays] = useState(7)
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
                onClick={() => setDays(d)}
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
