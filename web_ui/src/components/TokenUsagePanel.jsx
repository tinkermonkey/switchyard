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

export default function TokenUsagePanel({ logs, promptText }) {
  const [isUsageExpanded, setIsUsageExpanded] = useState(false)

  const tokenUsage = useMemo(() => {
    if (!logs || logs.length === 0) return { hasData: false }

    let firstInput = null
    let cumulativeLastInput = 0
    let cumulativeLastOutput = 0
    let lastCacheRead = 0
    let lastCacheCreation = 0
    let lastDirectInput = 0
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
        cumulativeLastInput = effectiveInput
        cumulativeLastOutput = outputTokens
        lastCacheRead = cacheRead
        lastCacheCreation = cacheCreation
        lastDirectInput = inputDirect

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
    const peakContext = cumulativeLastInput
    const contextGrowth = cumulativeLastInput - (firstInput || 0)

    return {
      hasData: firstInput !== null,
      initialInput: firstInput || 0,
      peakContext,
      contextGrowth,
      totalOutput: cumulativeLastOutput,
      totalCacheRead: lastCacheRead,
      totalCacheCreation: lastCacheCreation,
      totalDirectInput: lastDirectInput,
      totalAll: cumulativeLastInput + cumulativeLastOutput,
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
                  <td className="text-right font-mono">{formatTokenCount(tokenUsage.peakContext)}</td>
                </tr>
                <tr>
                  <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ direct input</td>
                  <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.totalDirectInput)}</td>
                </tr>
                <tr>
                  <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache reads</td>
                  <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.totalCacheRead)}</td>
                </tr>
                <tr>
                  <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache writes</td>
                  <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.totalCacheCreation)}</td>
                </tr>
                <tr className="border-t border-gh-border">
                  <td className="text-gh-fg-muted py-0.5">Output</td>
                  <td className="text-right font-mono">{formatTokenCount(tokenUsage.totalOutput)}</td>
                </tr>
                <tr className="border-t border-gh-border">
                  <td className="text-gh-fg font-semibold py-0.5">Grand total</td>
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
