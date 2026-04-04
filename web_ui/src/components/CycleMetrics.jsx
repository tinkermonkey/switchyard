import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, BarChart2 } from 'lucide-react'
import TrendChart from './TrendChart'

const DAYS_OPTIONS = [1, 3, 7, 14]  // server clamps cycle metrics to 14 days max

// Strip 'claude-' prefix and trailing 8-digit date from a model ID.
// e.g. 'claude-haiku-4-5-20251001' → 'haiku-4-5', 'claude-sonnet-4-6' → 'sonnet-4-6'
const formatModelName = (mod) => {
  const parts = mod.split('-')
  const withoutPrefix = parts[0] === 'claude' ? parts.slice(1) : parts
  const withoutDate = /^\d{8}$/.test(withoutPrefix[withoutPrefix.length - 1])
    ? withoutPrefix.slice(0, -1)
    : withoutPrefix
  return withoutDate.join('-')
}

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
  const label = cycle.cycle_type
  const tc = cycle.task_count || 0

  const agentBreakdown = Object.entries(cycle.agent_breakdown || {})
    .sort((a, b) => (b[1].task_count || 0) - (a[1].task_count || 0))

  const toolBreakdown = Object.entries(cycle.tool_breakdown || {})
    .sort((a, b) => (b[1].sum_context_growth || 0) - (a[1].sum_context_growth || 0))
    .slice(0, 10)

  const models = Object.keys(cycle.model_breakdown || {})

  return (
    <div className={`$ border rounded-md overflow-hidden`}>
      <div className={`px-4 py-3 border-b `}>
        <div className="flex items-center justify-between">
          <h3 className={`text-sm font-semibold `}>{label}</h3>
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
            label="Avg Peak Context"
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
                  {agentBreakdown.map(([agent, stats]) => {
                    const atc = stats.task_count || 0
                    const avgField = (f) => atc > 0 ? (stats[f] || 0) / atc : 0
                    return (
                      <tr key={agent}>
                        <td className="font-mono py-0.5">{agent}</td>
                        <td className="text-right py-0.5">{atc}</td>
                        <td className="text-right py-0.5 font-mono">{formatTokenCount(avgField('sum_direct_input'))}</td>
                        <td className="text-right py-0.5 font-mono">{formatTokenCount(avgField('sum_cache_read'))}</td>
                        <td className="text-right py-0.5 font-mono">{formatTokenCount(avgField('sum_output'))}</td>
                      </tr>
                    )
                  })}
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
                      {formatModelName(m)}
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
  const [hourlySeries, setHourlySeries] = useState({})
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
        setHourlySeries(data.hourly_series || {})
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

      {!loading && Object.keys(hourlySeries).length > 0 && (
        <TrendChart hourlySeries={hourlySeries} days={days} />
      )}

      {!loading && metrics.length > 0 && (
        <div className="grid grid-cols-1 gap-4">
          {metrics.map(cycle => (
            <CycleCard key={cycle.cycle_type} cycle={cycle} />
          ))}
        </div>
      )}

      {/* Metric Definitions */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-md p-4 mt-6">
        <h3 className="text-sm font-semibold text-gh-fg mb-3">Metric Definitions</h3>

        <div className="space-y-4 text-xs text-gh-fg-muted">
          <div>
            <p className="text-gh-fg font-semibold mb-1.5">Token Summary (per cycle card)</p>
            <table className="w-full">
              <tbody className="divide-y divide-gh-border">
                {[
                  ['Avg Direct Input', 'Average non-cached input tokens billed at full rate, summed across all turns per execution. For multi-turn cycles this accumulates with each API call and can exceed the initial context size.'],
                  ['Avg Cache Reads', 'Average tokens retrieved from the prompt cache per execution. Cheaper than direct input — reflects how effectively agents reuse prior context across turns within the cycle.'],
                  ['Avg Cache Creation', 'Average tokens written to the prompt cache per execution. Cache creation enables future cache reads but incurs a one-time write cost.'],
                  ['Avg Output', 'Average tokens generated by the model per execution. The range (min – max) shows the spread across all executions in the period.'],
                  ['Avg Peak Context', 'Average of the peak context window size reached during each execution. Context grows as tool results are appended; a high value indicates long-running or tool-heavy cycles. The range shows min and max across executions.'],
                  ['Avg Initial Context', 'Average context window size at the very start of each execution (system prompt + task description + pre-loaded history). Always reflects a single turn, so it can be smaller than Avg Direct Input for multi-turn cycles.'],
                ].map(([col, def]) => (
                  <tr key={col}>
                    <td className="py-1.5 pr-4 font-mono text-gh-fg whitespace-nowrap align-top w-44">{col}</td>
                    <td className="py-1.5">{def}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div>
            <p className="text-gh-fg font-semibold mb-1.5">Agent Breakdown (per cycle card)</p>
            <table className="w-full">
              <tbody className="divide-y divide-gh-border">
                {[
                  ['Avg Direct', 'Average direct (non-cached) input tokens per run when this agent participated in the cycle.'],
                  ['Avg Cache R', 'Average tokens read from the prompt cache per run when this agent participated in the cycle.'],
                  ['Avg Output', 'Average output tokens generated per run by this agent within the cycle.'],
                ].map(([col, def]) => (
                  <tr key={col}>
                    <td className="py-1.5 pr-4 font-mono text-gh-fg whitespace-nowrap align-top w-44">{col}</td>
                    <td className="py-1.5">{def}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div>
            <p className="text-gh-fg font-semibold mb-1.5">Tool Breakdown (per cycle card)</p>
            <table className="w-full">
              <tbody className="divide-y divide-gh-border">
                {[
                  ['Avg Output / Call', 'Average model output tokens produced per invocation of this tool (sum of output tokens attributed to calls of this tool ÷ invocation count). Helps identify which tools drive the most generation within the cycle.'],
                  ['Context Growth', 'Total tokens added to the context window across all executions of this cycle type by this tool\'s results. High values point to tools that return large payloads and consume context budget quickly.'],
                ].map(([col, def]) => (
                  <tr key={col}>
                    <td className="py-1.5 pr-4 font-mono text-gh-fg whitespace-nowrap align-top w-44">{col}</td>
                    <td className="py-1.5">{def}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}
