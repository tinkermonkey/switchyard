import { useState, useMemo } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'

const getToolResultLength = (content) => {
  if (!content) return 0
  if (typeof content === 'string') return content.length
  if (Array.isArray(content)) {
    return content.reduce((sum, item) => {
      if (typeof item === 'string') return sum + item.length
      if (item?.type === 'text') return sum + (item.text?.length || 0)
      return sum
    }, 0)
  }
  return 0
}

export default function TokenUsagePanel({ logs, summary, promptText }) {
  const [isUsageExpanded, setIsUsageExpanded] = useState(false)

  const tokenUsage = useMemo(() => {
    if (!logs || logs.length === 0) return { hasData: false }

    let firstInput = null
    let peakEffectiveInput = 0
    let totalOutputTokens = 0
    let peakCacheRead = 0
    let peakCacheCreation = 0
    let peakDirectInput = 0
    const modelsUsed = new Set()
    const toolCallCounts = {}
    const toolIdToName = {}
    const toolResultChars = {}
    const toolContextGrowthTokens = {}
    let prevEffectiveInput = null
    let prevTurnTools = []
    const tokenToolsAvailable = []
    const mcpServersAvailable = []

    for (const log of logs) {
      const evt = log.raw_event?.event
      if (!evt) continue

      if (evt.type === 'system' && evt.subtype === 'init') {
        if (Array.isArray(evt.tools) && tokenToolsAvailable.length === 0) {
          tokenToolsAvailable.push(...evt.tools.map(t => t.name || String(t)))
        }
        if (evt.mcp_servers && typeof evt.mcp_servers === 'object' && mcpServersAvailable.length === 0) {
          mcpServersAvailable.push(...Object.keys(evt.mcp_servers))
        }
      }

      if (evt.type === 'assistant' && evt.message?.usage) {
        const usage = evt.message.usage
        const model = evt.message.model
        if (model) modelsUsed.add(model)

        const inputDirect = usage.input_tokens || 0
        const cacheRead = usage.cache_read_input_tokens || 0
        const cacheCreation = usage.cache_creation_input_tokens || 0
        const effectiveInput = inputDirect + cacheRead + cacheCreation
        const outputTokens = usage.output_tokens || 0

        if (firstInput === null) firstInput = effectiveInput
        totalOutputTokens += outputTokens
        if (effectiveInput >= peakEffectiveInput) {
          peakEffectiveInput = effectiveInput
          peakCacheRead = cacheRead
          peakCacheCreation = cacheCreation
          peakDirectInput = inputDirect
        }

        if (prevEffectiveInput !== null && prevTurnTools.length > 0) {
          const delta = Math.max(0, effectiveInput - prevEffectiveInput)
          const perTool = delta / prevTurnTools.length
          for (const toolName of prevTurnTools) {
            toolContextGrowthTokens[toolName] = (toolContextGrowthTokens[toolName] || 0) + perTool
          }
        }

        const getEffectiveToolName = (item) => {
          if (item.name === 'Skill' && item.input?.skill) return item.input.skill
          if (item.name === 'Task' && item.input?.subagent_type) return item.input.subagent_type
          return item.name
        }
        const currentTurnTools = []
        const contents = Array.isArray(evt.message.content) ? evt.message.content : []
        for (const item of contents) {
          if (item.type === 'tool_use' && item.name) {
            const effectiveName = getEffectiveToolName(item)
            toolCallCounts[effectiveName] = (toolCallCounts[effectiveName] || 0) + 1
            if (item.id) toolIdToName[item.id] = effectiveName
            currentTurnTools.push(effectiveName)
          }
        }

        prevEffectiveInput = effectiveInput
        prevTurnTools = currentTurnTools
      }

      if (evt.type === 'user' && Array.isArray(evt.message?.content)) {
        for (const item of evt.message.content) {
          if (item.type === 'tool_result' && item.tool_use_id) {
            const toolName = toolIdToName[item.tool_use_id]
            if (toolName) {
              toolResultChars[toolName] = (toolResultChars[toolName] || 0) + getToolResultLength(item.content)
            }
          }
        }
      }
    }

    const promptLength = promptText?.length || 0
    const peakContext = peakEffectiveInput
    const contextGrowth = peakEffectiveInput - (firstInput || 0)

    return {
      hasData: firstInput !== null,
      initialInput: firstInput || 0,
      peakContext,
      contextGrowth,
      totalOutput: totalOutputTokens,
      peakCacheRead,
      peakCacheCreation,
      peakDirectInput,
      totalAll: peakEffectiveInput + totalOutputTokens,
      promptLength,
      modelsUsed: Array.from(modelsUsed),
      toolsAvailable: tokenToolsAvailable,
      mcpServersAvailable,
      toolsSummary: Object.entries(toolCallCounts)
        .map(([name, calls]) => ({
          name,
          calls,
          resultChars: toolResultChars[name] || 0,
          contextGrowthTokens: Math.round(toolContextGrowthTokens[name] || 0)
        }))
        .sort((a, b) => (b.contextGrowthTokens - a.contextGrowthTokens) || (b.resultChars - a.resultChars) || (b.calls - a.calls))
    }
  }, [logs, promptText])

  const formatTokenCount = (n) => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
  }

  const formatCost = (usd) => {
    if (!usd || usd === 0) return null
    if (usd < 0.01) return `$${usd.toFixed(4)}`
    return `$${usd.toFixed(2)}`
  }

  // Context window limits by model family. All current Claude models are 200k.
  const MODEL_CONTEXT_LIMITS = { default: 200_000 }
  const getContextLimit = (models) => {
    return MODEL_CONTEXT_LIMITS.default
  }

  // Pipeline-run-wide view: use pre-aggregated summary when available.
  // This is accurate across multiple agent executions (context resets properly,
  // tool counts are summed per execution rather than tracked as a single session).
  if (summary) {
    const totalInput = summary.total_input_tokens || 0
    const totalOutput = summary.total_output_tokens || 0
    const totalCost = summary.total_cost_usd || 0
    const cacheHitRate = totalInput > 0 ? Math.round((summary.total_cache_read || 0) / totalInput * 100) : 0
    const contextLimit = getContextLimit(summary.models_used)
    const peakUtilPct = contextLimit > 0 ? Math.round((summary.peak_context || 0) / contextLimit * 100) : null
    const agentBreakdown = summary.agent_breakdown || {}
    const agentRows = Object.entries(agentBreakdown)
      .map(([name, b]) => ({
        name,
        taskCount: b.task_count || 0,
        input: (b.sum_direct_input || 0) + (b.sum_cache_read || 0) + (b.sum_cache_creation || 0),
        cacheRead: b.sum_cache_read || 0,
        cacheCreation: b.sum_cache_creation || 0,
        output: b.sum_output || 0,
        toolCalls: b.tool_call_count || 0,
        cost: b.sum_cost_usd || 0,
      }))
      .sort((a, b) => b.input - a.input)

    const toolBreakdown = summary.tool_breakdown || {}
    const toolRows = Object.entries(toolBreakdown)
      .map(([name, td]) => ({
        name,
        calls: td.call_count || 0,
        contextGrowth: Math.round(td.sum_context_growth || 0),
      }))
      .sort((a, b) => (b.contextGrowth - a.contextGrowth) || (b.calls - a.calls))

    const modelsUsed = summary.models_used || []

    return (
      <div className="my-2 bg-gh-canvas-subtle rounded-md border border-gh-border">
        <button
          onClick={() => setIsUsageExpanded(!isUsageExpanded)}
          className="w-full flex items-center justify-between p-3 hover:bg-gh-border-muted transition-colors rounded-md"
        >
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-gh-fg">Token Usage</span>
            <span className="text-gh-fg-muted text-sm">·</span>
            <span className="text-sm text-gh-fg-muted">{formatTokenCount(totalInput + totalOutput)} total</span>
            <span className="text-gh-fg-muted text-sm">·</span>
            <span className="text-sm text-gh-fg-muted">{summary.task_count} execution{summary.task_count !== 1 ? 's' : ''}</span>
            {formatCost(totalCost) && (
              <>
                <span className="text-gh-fg-muted text-sm">·</span>
                <span className="text-sm text-gh-fg-muted">{formatCost(totalCost)}</span>
              </>
            )}
            {cacheHitRate > 0 && (
              <>
                <span className="text-gh-fg-muted text-sm">·</span>
                <span className="text-sm text-gh-fg-muted">{cacheHitRate}% cached</span>
              </>
            )}
          </div>
          {isUsageExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </button>

        {isUsageExpanded && (
          <div className="border-t border-gh-border p-3 grid grid-cols-3 gap-4">
            {/* Col 1: Totals */}
            <div>
              <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Pipeline Totals</h4>
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className="text-left text-gh-fg-muted font-normal text-xs pb-1"></th>
                    <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">tokens</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="text-gh-fg-muted py-0.5">Total input</td>
                    <td className="text-right font-mono">{formatTokenCount(totalInput)}</td>
                  </tr>
                  <tr>
                    <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ direct</td>
                    <td className="text-right font-mono text-xs">{formatTokenCount(summary.total_direct_input || 0)}</td>
                  </tr>
                  <tr>
                    <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache reads</td>
                    <td className="text-right font-mono text-xs">{formatTokenCount(summary.total_cache_read || 0)}</td>
                  </tr>
                  <tr>
                    <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache writes</td>
                    <td className="text-right font-mono text-xs">{formatTokenCount(summary.total_cache_creation || 0)}</td>
                  </tr>
                  <tr className="border-t border-gh-border">
                    <td className="text-gh-fg-muted py-0.5">Total output</td>
                    <td className="text-right font-mono">{formatTokenCount(totalOutput)}</td>
                  </tr>
                  <tr className="border-t border-gh-border">
                    <td className="text-gh-fg font-semibold py-0.5">Grand total</td>
                    <td className="text-right font-mono font-semibold">{formatTokenCount(totalInput + totalOutput)}</td>
                  </tr>
                  {summary.total_context_growth > 0 && (
                    <tr className="border-t border-gh-border">
                      <td className="text-gh-fg-muted py-0.5 pt-1.5">Cumulative ctx growth</td>
                      <td className="text-right font-mono pt-1.5">{formatTokenCount(summary.total_context_growth)}</td>
                    </tr>
                  )}
                  {summary.peak_context > 0 && (
                    <tr>
                      <td className="text-gh-fg-muted py-0.5">Peak context (single exec)</td>
                      <td className="text-right font-mono text-gh-fg-muted">
                        {formatTokenCount(summary.peak_context)}
                        {peakUtilPct !== null && <span className="ml-1 opacity-60">({peakUtilPct}%)</span>}
                      </td>
                    </tr>
                  )}
                  {totalInput > 0 && (
                    <tr>
                      <td className="text-gh-fg-muted py-0.5">Cache hit rate</td>
                      <td className="text-right font-mono">{cacheHitRate}%</td>
                    </tr>
                  )}
                  {summary.total_tool_calls > 0 && (
                    <tr>
                      <td className="text-gh-fg-muted py-0.5">Total tool calls</td>
                      <td className="text-right font-mono">{summary.total_tool_calls}</td>
                    </tr>
                  )}
                  {formatCost(totalCost) && (
                    <tr className="border-t border-gh-border">
                      <td className="text-gh-fg font-semibold py-0.5 pt-1.5">Est. cost</td>
                      <td className="text-right font-mono font-semibold pt-1.5">{formatCost(totalCost)}</td>
                    </tr>
                  )}
                </tbody>
              </table>

              {modelsUsed.length > 0 && (
                <div className="mt-3">
                  <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-1.5">Models</h4>
                  <div className="flex flex-wrap gap-1">
                    {modelsUsed.map(m => (
                      <span key={m} className="px-2 py-0.5 bg-gh-accent-subtle border border-gh-accent-muted rounded text-xs font-mono">
                        {m}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Col 2: Tool breakdown */}
            <div>
              <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tools Used</h4>
              {toolRows.length > 0 ? (
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className="text-left text-gh-fg-muted font-normal text-xs pb-1">Tool</th>
                      <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Calls</th>
                      <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Ctx growth (tok)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {toolRows.map(({ name, calls, contextGrowth }) => (
                      <tr key={name}>
                        <td className="text-gh-fg font-mono py-0.5 text-xs">{name}</td>
                        <td className="text-right text-gh-fg py-0.5">{calls}</td>
                        <td className="text-right text-gh-fg py-0.5">{contextGrowth > 0 ? formatTokenCount(contextGrowth) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-gh-fg-muted text-xs">No tool breakdown available</p>
              )}
            </div>

            {/* Col 3: Per-agent breakdown */}
            <div>
              <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">By Agent</h4>
              {agentRows.length > 0 ? (
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className="text-left text-gh-fg-muted font-normal text-xs pb-1">Agent</th>
                      <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Runs</th>
                      <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Input</th>
                      <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Output</th>
                      <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Tools</th>
                      {totalCost > 0 && <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Cost</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {agentRows.map(({ name, taskCount, input, output, toolCalls, cost }) => (
                      <tr key={name}>
                        <td className="text-gh-fg font-mono py-0.5 text-xs">{name}</td>
                        <td className="text-right text-gh-fg py-0.5">{taskCount}</td>
                        <td className="text-right text-gh-fg py-0.5">{formatTokenCount(input)}</td>
                        <td className="text-right text-gh-fg py-0.5">{formatTokenCount(output)}</td>
                        <td className="text-right text-gh-fg py-0.5">{toolCalls > 0 ? toolCalls : '—'}</td>
                        {totalCost > 0 && <td className="text-right text-gh-fg py-0.5">{formatCost(cost) || '—'}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-gh-fg-muted text-xs">No agent breakdown available</p>
              )}
            </div>
          </div>
        )}
      </div>
    )
  }

  if (!tokenUsage.hasData) return null

  return (
    <div className="my-2 bg-gh-canvas-subtle rounded-md border border-gh-border">
      <button
        onClick={() => setIsUsageExpanded(!isUsageExpanded)}
        className="w-full flex items-center justify-between p-3 hover:bg-gh-border-muted transition-colors rounded-md"
      >
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-gh-fg">Token Usage</span>
          <span className="text-gh-fg-muted text-sm">·</span>
          <span className="text-sm text-gh-fg-muted">
            {formatTokenCount(tokenUsage.totalAll)} total
          </span>
          {tokenUsage.modelsUsed.length > 0 && (
            <>
              <span className="text-gh-fg-muted text-sm">·</span>
              <span className="text-sm text-gh-fg-muted font-mono">{tokenUsage.modelsUsed[0]}</span>
            </>
          )}
          {tokenUsage.toolsSummary.length > 0 && (
            <>
              <span className="text-gh-fg-muted text-sm">·</span>
              <span className="text-sm text-gh-fg-muted">{tokenUsage.toolsSummary.length} tools used</span>
            </>
          )}
        </div>
        {isUsageExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
      </button>

      {isUsageExpanded && (
        <div className="border-t border-gh-border p-3 grid grid-cols-3 gap-4">
          {/* Col 1: Token counts */}
          <div>
            <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Token Counts</h4>
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className="text-left text-gh-fg-muted font-normal text-xs pb-1"></th>
                  <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">tokens</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="text-gh-fg-muted py-0.5">Initial context</td>
                  <td className="text-right font-mono">{formatTokenCount(tokenUsage.initialInput)}</td>
                </tr>
                <tr>
                  <td className="text-gh-fg-muted py-0.5">Context growth</td>
                  <td className="text-right font-mono">{formatTokenCount(tokenUsage.contextGrowth)}</td>
                </tr>
                <tr className="border-t border-gh-border">
                  <td className="text-gh-fg-muted py-0.5">Peak context</td>
                  <td className="text-right font-mono">
                    {formatTokenCount(tokenUsage.peakContext)}
                    {(() => {
                      const limit = getContextLimit(tokenUsage.modelsUsed)
                      const pct = limit > 0 ? Math.round(tokenUsage.peakContext / limit * 100) : null
                      return pct !== null ? <span className="ml-1 opacity-60 text-xs">({pct}%)</span> : null
                    })()}
                  </td>
                </tr>
                <tr>
                  <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ direct input</td>
                  <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.peakDirectInput)}</td>
                </tr>
                <tr>
                  <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache reads</td>
                  <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.peakCacheRead)}</td>
                </tr>
                <tr>
                  <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache writes</td>
                  <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.peakCacheCreation)}</td>
                </tr>
                {tokenUsage.peakContext > 0 && (
                  <tr>
                    <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache hit rate</td>
                    <td className="text-right font-mono text-xs">
                      {Math.round(tokenUsage.peakCacheRead / tokenUsage.peakContext * 100)}%
                    </td>
                  </tr>
                )}
                <tr className="border-t border-gh-border">
                  <td className="text-gh-fg-muted py-0.5">Output</td>
                  <td className="text-right font-mono">{formatTokenCount(tokenUsage.totalOutput)}</td>
                </tr>
                <tr className="border-t border-gh-border">
                  <td className="text-gh-fg font-semibold py-0.5">Peak ctx + output</td>
                  <td className="text-right font-mono font-semibold">{formatTokenCount(tokenUsage.totalAll)}</td>
                </tr>
                {tokenUsage.promptLength > 0 && (
                  <tr className="border-t border-gh-border">
                    <td className="text-gh-fg-muted py-0.5 pt-1.5">Input prompt</td>
                    <td className="text-right font-mono pt-1.5 text-gh-fg-muted">{formatTokenCount(tokenUsage.promptLength)} chars (~{formatTokenCount(Math.round(tokenUsage.promptLength / 4))} tok)</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Col 2: Models + Tools Available */}
          <div>
            <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Models</h4>
            <div className="flex flex-wrap gap-1 mb-3">
              {tokenUsage.modelsUsed.map(m => (
                <span key={m} className="px-2 py-0.5 bg-gh-accent-subtle border border-gh-accent-muted rounded text-xs font-mono">
                  {m}
                </span>
              ))}
            </div>
            {tokenUsage.toolsAvailable.length > 0 && (
              <>
                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tools Available</h4>
                <div className="flex flex-wrap gap-1">
                  {tokenUsage.toolsAvailable.map(t => {
                    const used = tokenUsage.toolsSummary.some(s => s.name === t)
                    return (
                      <span
                        key={t}
                        className={`px-2 py-0.5 rounded text-xs font-mono ${
                          used
                            ? 'bg-gh-warning-subtle border border-gh-warning text-gh-fg'
                            : 'bg-gh-canvas border border-gh-border text-gh-fg-muted'
                        }`}
                      >
                        {t}
                      </span>
                    )
                  })}
                </div>
              </>
            )}
          </div>

          {/* Col 3: Tools Used */}
          <div>
            <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tools Used</h4>
            {tokenUsage.toolsSummary.length > 0 ? (
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className="text-left text-gh-fg-muted font-normal text-xs pb-1">Tool</th>
                    <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Calls</th>
                    <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Result (chars)</th>
                    <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Ctx growth (tok)</th>
                  </tr>
                </thead>
                <tbody>
                  {tokenUsage.toolsSummary.map(({ name, calls, resultChars, contextGrowthTokens }) => (
                    <tr key={name}>
                      <td className="text-gh-fg font-mono py-0.5 text-xs">{name}</td>
                      <td className="text-right text-gh-fg py-0.5">{calls}</td>
                      <td className="text-right text-gh-fg py-0.5">{resultChars > 0 ? formatTokenCount(resultChars) : '—'}</td>
                      <td className="text-right text-gh-fg py-0.5">{contextGrowthTokens > 0 ? formatTokenCount(contextGrowthTokens) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="text-gh-fg-muted text-xs">No tool calls recorded</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
