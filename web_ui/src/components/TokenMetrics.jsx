import { useState, useEffect, useCallback } from 'react'
import { Coins, RefreshCw } from 'lucide-react'
import StackedTimeChart from './StackedTimeChart'

const DAYS_OPTIONS = [1, 3, 7, 14]

const fmtTokens = (n) => {
  if (n == null || n === 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(Math.round(n))
}

const fmtCost = (n) => {
  if (n == null || n === 0) return '$0.00'
  if (n >= 1000) return `$${n.toFixed(0)}`
  if (n >= 1) return `$${n.toFixed(2)}`
  if (n >= 0.01) return `$${n.toFixed(3)}`
  return `$${n.toFixed(4)}`
}

// Strip the 'claude-' prefix and trailing 8-digit date snapshot from a model ID.
const formatModelName = (mod) => {
  const parts = mod.split('-')
  const withoutPrefix = parts[0] === 'claude' ? parts.slice(1) : parts
  const withoutDate = /^\d{8}$/.test(withoutPrefix[withoutPrefix.length - 1])
    ? withoutPrefix.slice(0, -1)
    : withoutPrefix
  return withoutDate.join('-')
}

export default function TokenMetrics({ days, onDaysChange }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const fetchMetrics = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/metrics/tokens?days=${days}`)
      const json = await res.json()
      if (json.success) {
        setData(json)
      } else {
        setError(json.error || 'Failed to load token metrics')
      }
    } catch {
      setError('Failed to load token metrics')
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => { fetchMetrics() }, [fetchMetrics])

  const byModel = data?.by_model || {}
  const tokenSeries = data?.token_series || {}
  const costSeries = data?.cost_series || {}
  const agentExecSeries = data?.agent_exec_series || {}
  const pipelineRunSeries = data?.pipeline_run_series || {}

  // Build StackedTimeChart-compatible series: {displayName: [{h, value}]}
  const tokenChartSeries = Object.fromEntries(
    Object.entries(tokenSeries).map(([model, pts]) => [
      formatModelName(model),
      pts.map(p => ({ h: p.h, value: p.sum_tokens })),
    ])
  )
  const costChartSeries = Object.fromEntries(
    Object.entries(costSeries).map(([model, pts]) => [
      formatModelName(model),
      pts.map(p => ({ h: p.h, value: p.sum_cost })),
    ])
  )

  // Sort models by total cost desc for the table
  const modelRows = Object.entries(byModel)
    .map(([model, m]) => ({
      model,
      displayName: formatModelName(model),
      taskCount: m.task_count || 0,
      sumDirectInput: m.sum_direct_input || 0,
      sumCacheRead: m.sum_cache_read || 0,
      sumCacheCreation: m.sum_cache_creation || 0,
      sumOutput: m.sum_output || 0,
      sumCostUsd: m.sum_cost_usd || 0,
    }))
    .sort((a, b) => b.sumCostUsd - a.sumCostUsd)

  const totals = modelRows.reduce(
    (acc, r) => ({
      taskCount: acc.taskCount + r.taskCount,
      sumDirectInput: acc.sumDirectInput + r.sumDirectInput,
      sumCacheRead: acc.sumCacheRead + r.sumCacheRead,
      sumCacheCreation: acc.sumCacheCreation + r.sumCacheCreation,
      sumOutput: acc.sumOutput + r.sumOutput,
      sumCostUsd: acc.sumCostUsd + r.sumCostUsd,
    }),
    { taskCount: 0, sumDirectInput: 0, sumCacheRead: 0, sumCacheCreation: 0, sumOutput: 0, sumCostUsd: 0 }
  )

  const hasData = modelRows.length > 0

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Coins className="w-5 h-5 text-gh-accent-primary" />
          <h2 className="text-lg font-semibold">Token Metrics</h2>
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

      {!loading && !hasData && (
        <div className="p-8 bg-gh-canvas-subtle border border-gh-border rounded-md text-center">
          <Coins className="w-8 h-8 text-gh-fg-muted mx-auto mb-3" />
          <p className="text-gh-fg-muted text-sm font-medium mb-1">No token metrics data yet</p>
          <p className="text-gh-fg-muted text-xs">
            Token metrics are computed by a scheduled job (every 3 hours by default).
            Check back after the job has run, or trigger it via the Agent Metrics page.
          </p>
        </div>
      )}

      {!loading && hasData && (
        <>
          {/* Token usage chart */}
          {Object.keys(tokenChartSeries).length > 0 && (
            <StackedTimeChart
              series={tokenChartSeries}
              days={days}
              formatValue={fmtTokens}
              title="Total tokens consumed over time — stacked by model (input + cache + output)"
            />
          )}

          {/* Cost chart */}
          {Object.keys(costChartSeries).length > 0 && (
            <StackedTimeChart
              series={costChartSeries}
              days={days}
              formatValue={fmtCost}
              title="Total cost over time — stacked by model (USD)"
            />
          )}

          {/* Per-model summary table */}
          <div className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="border-b border-gh-border">
                  <tr className="text-gh-fg-muted text-xs font-semibold uppercase">
                    <th className="px-3 py-2 text-left">Model</th>
                    <th className="px-3 py-2 text-right">Executions</th>
                    <th className="px-3 py-2 text-right">Total Input</th>
                    <th className="px-3 py-2 text-right">Total Cache Read</th>
                    <th className="px-3 py-2 text-right">Total Cache Creation</th>
                    <th className="px-3 py-2 text-right">Total Output</th>
                    <th className="px-3 py-2 text-right">Total Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gh-border">
                  {modelRows.map(r => (
                    <tr key={r.model} className="hover:bg-gh-canvas transition-colors">
                      <td className="px-3 py-2">
                        <span className="font-mono text-sm">{r.displayName}</span>
                      </td>
                      <td className="px-3 py-2 text-sm text-right font-mono">{r.taskCount.toLocaleString()}</td>
                      <td className="px-3 py-2 text-sm text-right font-mono">{fmtTokens(r.sumDirectInput)}</td>
                      <td className="px-3 py-2 text-sm text-right font-mono">{fmtTokens(r.sumCacheRead)}</td>
                      <td className="px-3 py-2 text-sm text-right font-mono">{fmtTokens(r.sumCacheCreation)}</td>
                      <td className="px-3 py-2 text-sm text-right font-mono">{fmtTokens(r.sumOutput)}</td>
                      <td className="px-3 py-2 text-sm text-right font-mono font-semibold text-gh-fg">
                        {fmtCost(r.sumCostUsd)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                {modelRows.length > 1 && (
                  <tfoot className="border-t-2 border-gh-border bg-gh-canvas">
                    <tr className="text-sm font-semibold">
                      <td className="px-3 py-2">Total</td>
                      <td className="px-3 py-2 text-right font-mono">{totals.taskCount.toLocaleString()}</td>
                      <td className="px-3 py-2 text-right font-mono">{fmtTokens(totals.sumDirectInput)}</td>
                      <td className="px-3 py-2 text-right font-mono">{fmtTokens(totals.sumCacheRead)}</td>
                      <td className="px-3 py-2 text-right font-mono">{fmtTokens(totals.sumCacheCreation)}</td>
                      <td className="px-3 py-2 text-right font-mono">{fmtTokens(totals.sumOutput)}</td>
                      <td className="px-3 py-2 text-right font-mono text-gh-fg">{fmtCost(totals.sumCostUsd)}</td>
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
          </div>

          {/* Note about cost data source */}
          <p className="text-xs text-gh-fg-muted px-1">
            Cost is derived from <code className="font-mono">cost_usd</code> reported per API call by the Claude OTEL exporter and attributed to the model used in that call.
            Re-run the token metrics job after new executions to refresh this data.
          </p>

          {/* Pipeline runs over time by project */}
          {Object.keys(pipelineRunSeries).length > 0 && (
            <StackedTimeChart
              series={Object.fromEntries(
                Object.entries(pipelineRunSeries).map(([project, pts]) => [
                  project,
                  pts.map(p => ({ h: p.h, value: p.count })),
                ])
              )}
              days={days}
              formatValue={(n) => (n == null ? '0' : String(Math.round(n)))}
              title="Total pipeline runs over time — stacked by project"
            />
          )}

          {/* Agent executions over time by agent type */}
          {Object.keys(agentExecSeries).length > 0 && (
            <StackedTimeChart
              series={Object.fromEntries(
                Object.entries(agentExecSeries).map(([agent, pts]) => [
                  agent,
                  pts.map(p => ({ h: p.h, value: p.count })),
                ])
              )}
              days={days}
              formatValue={(n) => (n == null ? '0' : String(Math.round(n)))}
              title="Total agent executions over time — stacked by agent type"
            />
          )}
        </>
      )}
    </div>
  )
}
