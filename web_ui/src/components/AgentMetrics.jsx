import { Fragment, useState, useEffect, useCallback } from 'react'
import { Link } from '@tanstack/react-router'
import { RefreshCw, BarChart2, ChevronDown, ChevronRight } from 'lucide-react'
import TrendChart from './TrendChart'

const DAYS_OPTIONS = [1, 3, 7, 14]

const formatTokenCount = (n) => {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(Math.round(n))
}


// Derive avg fields from an agent metrics doc returned by the API.
// The API returns both raw sums and pre-computed avg_* fields.
const getAvg = (m, field) => m[field] ?? 0
const getCount = (m) => m.task_count ?? 0

// Strip the 'claude-' prefix and trailing 8-digit date snapshot from a model ID.
// e.g. 'claude-haiku-4-5-20251001' → 'haiku-4-5'
//      'claude-sonnet-4-6'          → 'sonnet-4-6'
const formatModelName = (mod) => {
  const parts = mod.split('-')
  const withoutPrefix = parts[0] === 'claude' ? parts.slice(1) : parts
  const withoutDate = /^\d{8}$/.test(withoutPrefix[withoutPrefix.length - 1])
    ? withoutPrefix.slice(0, -1)
    : withoutPrefix
  return withoutDate.join('-')
}

const StatCell = ({ value, sub }) => (
  <td className="px-3 py-2 text-sm">
    <div className="font-mono">{formatTokenCount(value)}</div>
    {sub != null && (
      <div className="text-xs text-gh-fg-muted font-mono">{sub}</div>
    )}
  </td>
)

export default function AgentMetrics({ days, onDaysChange }) {
  const [metrics, setMetrics] = useState([])
  const [hourlySeries, setHourlySeries] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [sortField, setSortField] = useState('avg_output')
  const [sortDir, setSortDir] = useState('desc')
  const [expandedAgents, setExpandedAgents] = useState(new Set())
  const [topExecutions, setTopExecutions] = useState(null)

  const fetchMetrics = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [agentRes, topRes] = await Promise.all([
        fetch(`/api/metrics/agents?days=${days}`),
        fetch(`/api/metrics/executions/top?days=${days}`),
      ])
      const [data, topData] = await Promise.all([agentRes.json(), topRes.json()])
      if (data.success) {
        setMetrics(data.metrics || [])
        setHourlySeries(data.hourly_series || {})
      } else {
        setError(data.error || 'Failed to load agent metrics')
      }
      if (topData.success) {
        setTopExecutions({
          prompt: topData.top_initial_context || [],
          peak: topData.top_peak_context || [],
          tools: topData.top_tool_call_count || [],
        })
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
    const av = getAvg(a, sortField)
    const bv = getAvg(b, sortField)
    if (typeof av === 'string') {
      return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av)
    }
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

      {sorted.length > 0 && (
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-gh-border">
                <tr>
                  <th className="px-3 py-2 w-6" />
                  <SortHeader field="agent_name" label="Agent" />
                  <SortHeader field="task_count" label="Executions" />
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg Direct Input</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg Cache Reads</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Avg Cache Creation</th>
                  <SortHeader field="avg_output" label="Avg Output" />
                  <SortHeader field="avg_max_context" label="Avg Max Context" />
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Models</th>
                  <th className="px-3 py-2 text-left text-xs font-semibold text-gh-fg-muted uppercase">Top 3 Tools</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gh-border">
                {sorted.map(m => {
                  const topTools = Object.entries(m.tool_breakdown || {})
                    .sort((a, b) => (b[1].task_count || 0) - (a[1].task_count || 0))
                    .slice(0, 3)
                  const models = Object.keys(m.model_breakdown || {})
                  const isExpanded = expandedAgents.has(m.agent_name)
                  const tc = getCount(m)

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
                        <td className="px-3 py-2 text-sm text-center">{tc}</td>
                        <StatCell value={getAvg(m, 'avg_direct_input')} />
                        <StatCell value={getAvg(m, 'avg_cache_read')} />
                        <StatCell value={getAvg(m, 'avg_cache_creation')} />
                        <StatCell
                          value={getAvg(m, 'avg_output')}
                          sub={`${formatTokenCount(m.min_output)}–${formatTokenCount(m.max_output)}`}
                        />
                        <StatCell
                          value={getAvg(m, 'avg_max_context')}
                          sub={`${formatTokenCount(m.min_max_context)}–${formatTokenCount(m.max_max_context)}`}
                        />
                        <td className="px-3 py-2">
                          <div className="flex flex-wrap gap-1">
                            {models.map(mod => (
                              <span key={mod} className="px-1.5 py-0.5 bg-gh-accent-subtle border border-gh-accent-muted rounded text-xs font-mono">
                                {formatModelName(mod)}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td className="px-3 py-2">
                          <div className="space-y-0.5">
                            {topTools.map(([name, tb]) => (
                              <div key={name} className="text-xs font-mono">
                                <span className="text-gh-fg-muted">{name}</span>
                                <span className="text-gh-fg">×{tb.task_count || 0}</span>
                                {tc > 0 && (
                                  <span className="text-gh-fg-muted ml-1">
                                    ({((tb.task_count || 0) / tc).toFixed(1)}/exec)
                                  </span>
                                )}
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="bg-gh-canvas">
                          <td colSpan={10} className="px-4 py-3">
                            <div className="grid grid-cols-2 gap-6">
                              {/* Tool Breakdown */}
                              <div>
                                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tool Breakdown</h4>
                                {Object.keys(m.tool_breakdown || {}).length > 0 ? (
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
                                      {Object.entries(m.tool_breakdown)
                                        .sort((a, b) => (b[1].sum_context_growth || 0) - (a[1].sum_context_growth || 0))
                                        .map(([tool, tb]) => {
                                          const inv = tb.task_count || 0
                                          return (
                                            <tr key={tool}>
                                              <td className="font-mono py-0.5">{tool}</td>
                                              <td className="text-right py-0.5">{inv}</td>
                                              <td className="text-right py-0.5 font-mono">
                                                {inv > 0 ? formatTokenCount((tb.sum_output || 0) / inv) : '—'}
                                              </td>
                                              <td className="text-right py-0.5 font-mono">
                                                {formatTokenCount(tb.sum_context_growth)}
                                              </td>
                                            </tr>
                                          )
                                        })
                                      }
                                    </tbody>
                                  </table>
                                ) : (
                                  <p className="text-gh-fg-muted text-xs">No tool data</p>
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
                                        <th className="text-right font-normal pb-1">Tasks</th>
                                        <th className="text-right font-normal pb-1">Avg Direct</th>
                                        <th className="text-right font-normal pb-1">Avg Cache R</th>
                                        <th className="text-right font-normal pb-1">Avg Output</th>
                                        <th className="text-right font-normal pb-1">Avg Max Ctx</th>
                                      </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gh-border">
                                      {models.map(mod => {
                                        const mb = m.model_breakdown[mod]
                                        const mtc = mb.task_count || 0
                                        return (
                                          <tr key={mod}>
                                            <td className="font-mono py-0.5">{formatModelName(mod)}</td>
                                            <td className="text-right py-0.5">{mtc}</td>
                                            <td className="text-right py-0.5 font-mono">
                                              {formatTokenCount(mtc > 0 ? (mb.sum_direct_input || 0) / mtc : 0)}
                                            </td>
                                            <td className="text-right py-0.5 font-mono">
                                              {formatTokenCount(mtc > 0 ? (mb.sum_cache_read || 0) / mtc : 0)}
                                            </td>
                                            <td className="text-right py-0.5 font-mono">
                                              {formatTokenCount(mtc > 0 ? (mb.sum_output || 0) / mtc : 0)}
                                            </td>
                                            <td className="text-right py-0.5 font-mono">
                                              {formatTokenCount(mtc > 0 ? (mb.sum_max_context || 0) / mtc : 0)}
                                            </td>
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

      {/* Top-10 Execution Tables */}
      {topExecutions && (() => {
        const allEmpty = topExecutions.prompt.length === 0 && topExecutions.peak.length === 0 && topExecutions.tools.length === 0
        const ExecLink = ({ row, children }) =>
          row.agent_execution_id ? (
            <Link
              to="/agent-execution/$executionId"
              params={{ executionId: row.agent_execution_id }}
              search={{ autoAdvance: false }}
              className="text-gh-accent-primary hover:underline"
            >
              {children}
            </Link>
          ) : <span>{children}</span>

        const fmtDate = (ts) => {
          if (!ts) return '—'
          try { return new Date(ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
          catch { return ts }
        }

        const TopTable = ({ title, rows, metricLabel, metricValue }) => (
          <div className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden">
            <div className="px-3 py-2 border-b border-gh-border">
              <h3 className="text-sm font-semibold text-gh-fg">{title}</h3>
            </div>
            {rows.length === 0 ? (
              <p className="px-3 py-4 text-xs text-gh-fg-muted text-center">No data yet — metrics are computed by the token metrics job</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="border-b border-gh-border">
                    <tr className="text-gh-fg-muted">
                      <th className="px-3 py-1.5 text-left font-semibold uppercase">#</th>
                      <th className="px-3 py-1.5 text-left font-semibold uppercase">Agent</th>
                      <th className="px-3 py-1.5 text-right font-semibold uppercase">{metricLabel}</th>
                      <th className="px-3 py-1.5 text-left font-semibold uppercase">Project</th>
                      <th className="px-3 py-1.5 text-left font-semibold uppercase">Started</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gh-border">
                    {rows.map((row, i) => (
                      <tr key={row.task_id || i} className="hover:bg-gh-canvas transition-colors">
                        <td className="px-3 py-1.5 text-gh-fg-muted">{i + 1}</td>
                        <td className="px-3 py-1.5 font-mono">
                          <ExecLink row={row}>{row.agent_name || '—'}</ExecLink>
                        </td>
                        <td className="px-3 py-1.5 text-right font-mono">{metricValue(row)}</td>
                        <td className="px-3 py-1.5 text-gh-fg-muted">{row.project || '—'}</td>
                        <td className="px-3 py-1.5 text-gh-fg-muted whitespace-nowrap">{fmtDate(row.started_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )

        return (
          <div className="space-y-4 mt-6">
            <h2 className="text-base font-semibold text-gh-fg">Top 10 Executions</h2>
            {allEmpty && (
              <p className="text-sm text-gh-fg-muted">No execution summary data yet — metrics are computed by the token metrics job</p>
            )}
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
              <TopTable
                title="Largest Initial Prompt"
                rows={topExecutions.prompt}
                metricLabel="Initial Context"
                metricValue={(r) => formatTokenCount(r.initial_context)}
              />
              <TopTable
                title="Highest Peak Context"
                rows={topExecutions.peak}
                metricLabel="Peak Context"
                metricValue={(r) => formatTokenCount(r.peak_context)}
              />
              <TopTable
                title="Most Tool Calls"
                rows={topExecutions.tools}
                metricLabel="Tool Calls"
                metricValue={(r) => r.tool_call_count ?? '—'}
              />
            </div>
          </div>
        )
      })()}

      {/* Metric Definitions */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-md p-4 mt-6">
        <h3 className="text-sm font-semibold text-gh-fg mb-3">Metric Definitions</h3>

        <div className="space-y-4 text-xs text-gh-fg-muted">
          <div>
            <p className="text-gh-fg font-semibold mb-1.5">Main Table</p>
            <table className="w-full">
              <tbody className="divide-y divide-gh-border">
                {[
                  ['Avg Direct Input', 'Average tokens sent in the prompt that are not cache hits, per execution. Billed at full input rate.'],
                  ['Avg Cache Reads', 'Average tokens retrieved from the prompt cache per execution. Cheaper than direct input — reflects how effectively the agent reuses prior context.'],
                  ['Avg Cache Creation', 'Average tokens written to the prompt cache per execution. Cache creation enables future cache reads but incurs a one-time write cost.'],
                  ['Avg Output', 'Average tokens generated by the model per execution. The smaller gray range (e.g. 1.2K–8.4K) shows the min and max observed across all executions in the period.'],
                  ['Avg Max Context', 'Average of the peak context window size reached during each execution. Context accumulates as tool results are appended; a high value may indicate long-running or tool-heavy tasks. The range shows min and max across executions.'],
                ].map(([col, def]) => (
                  <tr key={col}>
                    <td className="py-1.5 pr-4 font-mono text-gh-fg whitespace-nowrap align-top w-40">{col}</td>
                    <td className="py-1.5">{def}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div>
            <p className="text-gh-fg font-semibold mb-1.5">Tool Breakdown (expanded row)</p>
            <p className="text-gh-fg-muted text-xs mb-2">Note: token counts per tool are an equal-split approximation — when multiple tools are called in one turn, that turn&apos;s tokens are divided equally across them. The Claude API does not attribute tokens to individual tool calls.</p>
            <table className="w-full">
              <tbody className="divide-y divide-gh-border">
                {[
                  ['Avg Output / Call', 'Average model output tokens produced per invocation of this tool (sum of output tokens attributed to calls of this tool ÷ invocation count). Helps identify which tools drive the most generation.'],
                  ['Context Growth', 'Total tokens added to the context window across all executions by this tool\'s results. Tool results are appended to the conversation, growing the context; high values point to tools that return large payloads.'],
                ].map(([col, def]) => (
                  <tr key={col}>
                    <td className="py-1.5 pr-4 font-mono text-gh-fg whitespace-nowrap align-top w-40">{col}</td>
                    <td className="py-1.5">{def}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div>
            <p className="text-gh-fg font-semibold mb-1.5">Model Breakdown (expanded row)</p>
            <table className="w-full">
              <tbody className="divide-y divide-gh-border">
                {[
                  ['Avg Direct', 'Average direct (non-cached) input tokens per task when this model was used.'],
                  ['Avg Cache R', 'Average tokens read from the prompt cache per task when this model was used.'],
                  ['Avg Output', 'Average output tokens generated per task by this model.'],
                  ['Avg Max Ctx', 'Average peak context window size per task when this model was used.'],
                ].map(([col, def]) => (
                  <tr key={col}>
                    <td className="py-1.5 pr-4 font-mono text-gh-fg whitespace-nowrap align-top w-40">{col}</td>
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
