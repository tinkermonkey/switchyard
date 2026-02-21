import { Fragment, useState, useEffect, useCallback } from 'react'
import { RefreshCw, BarChart2, ChevronDown, ChevronRight } from 'lucide-react'

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
  const [expandedAgents, setExpandedAgents] = useState(new Set())

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

  const toggleAgent = (agentName) => {
    setExpandedAgents(prev => {
      const next = new Set(prev)
      if (next.has(agentName)) {
        next.delete(agentName)
      } else {
        next.add(agentName)
      }
      return next
    })
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
                  <th className="px-3 py-2 w-6" />
                  <SortHeader field="agent_name" label="Agent" />
                  <SortHeader field="sample_count" label="Executions" />
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg/Min/Max Initial</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg/Min/Max Input</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg/Min/Max Output</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg/Min/Max Total</th>
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
                  const isExpanded = expandedAgents.has(m.agent_name)

                  return (
                    <Fragment key={m.agent_name}>
                      <tr
                        className="hover:bg-gh-canvas transition-colors cursor-pointer"
                        onClick={() => toggleAgent(m.agent_name)}
                      >
                        <td className="px-3 py-2 text-gh-fg-muted">
                          {isExpanded
                            ? <ChevronDown className="w-3.5 h-3.5" />
                            : <ChevronRight className="w-3.5 h-3.5" />
                          }
                        </td>
                        <td className="px-3 py-2">
                          <span className="font-mono text-sm text-gh-fg">{m.agent_name}</span>
                        </td>
                        <td className="px-3 py-2 text-sm text-center">{m.sample_count}</td>
                        <StatCell avg={m.avg_initial_input} min={m.min_initial_input} max={m.max_initial_input} />
                        <StatCell avg={m.avg_total_input} min={m.min_total_input} max={m.max_total_input} />
                        <StatCell avg={m.avg_total_output} min={m.min_total_output} max={m.max_total_output} />
                        <StatCell avg={m.avg_total_all} min={m.min_total_all} max={m.max_total_all} />
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
                          <div className="space-y-0.5">
                            {topTools.map(([name, count]) => (
                              <div key={name} className="text-xs font-mono">
                                <span className="text-gh-fg-muted">{name}</span>
                                <span className="text-gh-fg">×{count}</span>
                                {m.sample_count > 0 && (
                                  <span className="text-gh-fg-muted ml-1">
                                    ({(count / m.sample_count).toFixed(1)}/exec)
                                  </span>
                                )}
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="bg-gh-canvas">
                          <td colSpan={9} className="px-4 py-3">
                            <div className="grid grid-cols-2 gap-6">
                              {/* Tool Token Attribution */}
                              <div>
                                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tool Token Attribution</h4>
                                {Object.keys(m.tool_token_attribution || {}).length > 0 ? (
                                  <table className="w-full text-xs">
                                    <thead>
                                      <tr className="text-gh-fg-muted">
                                        <th className="text-left font-normal pb-1">Tool</th>
                                        <th className="text-right font-normal pb-1">Total Calls</th>
                                        <th className="text-right font-normal pb-1">Avg Calls/Exec</th>
                                        <th className="text-right font-normal pb-1">Avg Tokens/Call</th>
                                      </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gh-border">
                                      {Object.entries(m.tool_token_attribution)
                                        .sort((a, b) => b[1].total_tokens - a[1].total_tokens)
                                        .map(([tool, attr]) => (
                                          <tr key={tool}>
                                            <td className="font-mono py-0.5">{tool}</td>
                                            <td className="text-right py-0.5">{attr.call_count}</td>
                                            <td className="text-right py-0.5 font-mono">
                                              {m.sample_count > 0
                                                ? (attr.call_count / m.sample_count).toFixed(1)
                                                : '—'}
                                            </td>
                                            <td className="text-right py-0.5 font-mono">{formatTokenCount(attr.avg_tokens_per_call)}</td>
                                          </tr>
                                        ))
                                      }
                                    </tbody>
                                  </table>
                                ) : (
                                  <p className="text-gh-fg-muted text-xs">No attribution data</p>
                                )}
                              </div>

                              {/* Model Breakdown */}
                              <div>
                                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Model Breakdown</h4>
                                {models.length > 0 ? (
                                  <table className="w-full text-xs">
                                    <thead>
                                      <tr className="text-gh-fg-muted">
                                        <th className="text-left font-normal pb-1">Model</th>
                                        <th className="text-right font-normal pb-1">Executions</th>
                                        <th className="text-right font-normal pb-1">Avg Total</th>
                                        <th className="text-right font-normal pb-1">Min</th>
                                        <th className="text-right font-normal pb-1">Max</th>
                                      </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gh-border">
                                      {models.map(mod => {
                                        const mb = m.model_breakdown[mod]
                                        // Support both old format (number) and new format (object)
                                        const taskCount = typeof mb === 'object' ? mb.task_count : mb
                                        const avgTotal = typeof mb === 'object' ? mb.avg_total : null
                                        const minTotal = typeof mb === 'object' ? mb.min_total : null
                                        const maxTotal = typeof mb === 'object' ? mb.max_total : null
                                        return (
                                          <tr key={mod}>
                                            <td className="font-mono py-0.5">{mod.split('-').slice(-2).join('-')}</td>
                                            <td className="text-right py-0.5">{taskCount}</td>
                                            <td className="text-right py-0.5 font-mono">{formatTokenCount(avgTotal)}</td>
                                            <td className="text-right py-0.5 font-mono">{formatTokenCount(minTotal)}</td>
                                            <td className="text-right py-0.5 font-mono">{formatTokenCount(maxTotal)}</td>
                                          </tr>
                                        )
                                      })}
                                    </tbody>
                                  </table>
                                ) : (
                                  <p className="text-gh-fg-muted text-xs">No model data</p>
                                )}
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
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
