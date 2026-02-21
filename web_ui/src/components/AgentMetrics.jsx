import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, BarChart2 } from 'lucide-react'

const DAYS_OPTIONS = [1, 7, 30]

const formatTokenCount = (n) => {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

const StatCell = ({ avg, min, max }) => (
  <td className="px-3 py-2 text-sm">
    <div className="font-mono">{formatTokenCount(avg)}</div>
    {(min != null || max != null) && (
      <div className="text-xs text-gh-fg-muted font-mono">
        {formatTokenCount(min)} – {formatTokenCount(max)}
      </div>
    )}
  </td>
)

export default function AgentMetrics() {
  const [days, setDays] = useState(7)
  const [metrics, setMetrics] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [sortField, setSortField] = useState('avg_total_all')
  const [sortDir, setSortDir] = useState('desc')

  const fetchMetrics = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/metrics/agents?days=${days}`)
      const data = await res.json()
      if (data.success) {
        setMetrics(data.metrics || [])
      } else {
        setError(data.error || 'Failed to load agent metrics')
      }
    } catch (err) {
      setError('Failed to load agent metrics')
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => {
    fetchMetrics()
  }, [fetchMetrics])

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDir('desc')
    }
  }

  const sorted = [...metrics].sort((a, b) => {
    const av = a[sortField] ?? 0
    const bv = b[sortField] ?? 0
    return sortDir === 'asc' ? av - bv : bv - av
  })

  const SortHeader = ({ field, label }) => (
    <th
      className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase cursor-pointer hover:text-gh-fg select-none"
      onClick={() => handleSort(field)}
    >
      {label}
      {sortField === field && (
        <span className="ml-1">{sortDir === 'asc' ? '↑' : '↓'}</span>
      )}
    </th>
  )

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart2 className="w-5 h-5 text-gh-accent-primary" />
          <h2 className="text-lg font-semibold">Agent Token Metrics</h2>
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

      {sorted.length > 0 && (
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-gh-border">
                <tr>
                  <SortHeader field="agent_name" label="Agent" />
                  <SortHeader field="sample_count" label="Executions" />
                  <SortHeader field="avg_initial_input" label="Avg Initial Prompt" />
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg/Min/Max Input</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg/Min/Max Output</th>
                  <SortHeader field="avg_total_all" label="Avg Total" />
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Models</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Top 3 Tools</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gh-border">
                {sorted.map(m => {
                  const topTools = Object.entries(m.tool_call_counts || {})
                    .sort((a, b) => b[1] - a[1])
                    .slice(0, 3)
                  const models = Object.keys(m.model_breakdown || {})

                  return (
                    <tr key={m.agent_name} className="hover:bg-gh-canvas transition-colors">
                      <td className="px-3 py-2">
                        <span className="font-mono text-sm text-gh-fg">{m.agent_name}</span>
                      </td>
                      <td className="px-3 py-2 text-sm text-center">{m.sample_count}</td>
                      <td className="px-3 py-2 text-sm font-mono">{formatTokenCount(m.avg_initial_input)}</td>
                      <StatCell avg={m.avg_total_input} min={m.min_total_input} max={m.max_total_input} />
                      <StatCell avg={m.avg_total_output} min={m.min_total_output} max={m.max_total_output} />
                      <td className="px-3 py-2 text-sm font-mono font-semibold">{formatTokenCount(m.avg_total_all)}</td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap gap-1">
                          {models.map(mod => (
                            <span key={mod} className="px-1.5 py-0.5 bg-gh-accent-subtle border border-gh-accent-muted rounded text-xs font-mono">
                              {mod.split('-').slice(-2).join('-')}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap gap-1">
                          {topTools.map(([name, count]) => (
                            <span key={name} className="text-xs text-gh-fg-muted font-mono">
                              {name}×{count}
                            </span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

    </div>
  )
}
