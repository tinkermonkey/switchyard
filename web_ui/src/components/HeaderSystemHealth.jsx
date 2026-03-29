/**
 * HeaderSystemHealth - Shows GitHub API and Claude token usage metrics
 */
import { Activity, Zap, AlertTriangle } from 'lucide-react'
import HeaderBox from './HeaderBox'
import { useSystemHealth } from '../hooks/useSystemHealth'

// Format large token counts compactly: 1234567 -> "1.2M", 1234 -> "1,234"
const formatTokens = (num) => {
  if (!num && num !== 0) return 'N/A'
  if (num >= 1_000_000_000) return `${(num / 1_000_000_000).toFixed(1)}B`
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`
  if (num >= 10_000) return `${(num / 1_000).toFixed(0)}K`
  return num.toLocaleString()
}

export default function HeaderSystemHealth() {
  const { checks, loading } = useSystemHealth()

  if (loading || !checks) {
    return (
      <HeaderBox title="API Usage" minWidth="md:min-w-[240px]">
        <p className="text-xs text-gh-fg-muted">Loading...</p>
      </HeaderBox>
    )
  }

  const githubCheck = checks.github
  const claudeCheck = checks.claude

  // Helper to format large numbers with commas
  const formatNumber = (num) => {
    if (!num && num !== 0) return 'N/A'
    return num.toLocaleString()
  }

  // Helper to get color based on percentage used (for GitHub API only)
  const getUsageColor = (percentageUsed) => {
    if (percentageUsed >= 90) return 'text-gh-danger'
    if (percentageUsed >= 75) return 'text-yellow-500'
    return 'text-gh-success'
  }

  const tokenUsage = claudeCheck?.token_usage
  const tokenStatus = tokenUsage?.status

  return (
    <HeaderBox title="API Usage" minWidth="md:min-w-[180px]">
      <div className="space-y-2.5">
        {/* GitHub API Usage */}
        {githubCheck?.api_usage && (
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-xs">
              <Activity className="w-3 h-3 text-gh-fg-muted" />
              <span className="text-gh-fg-default font-medium">GitHub API</span>
            </div>
            <div className="pl-4.5 space-y-0.5">
              <div className="flex items-center justify-between text-xs">
                <span className="text-gh-fg-muted">Remaining</span>
                <span className={getUsageColor(githubCheck.api_usage.percentage_used)}>
                  {formatNumber(githubCheck.api_usage.remaining)} / {formatNumber(githubCheck.api_usage.limit)}
                </span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-gh-fg-muted">Used</span>
                <span className={getUsageColor(githubCheck.api_usage.percentage_used)}>
                  {githubCheck.api_usage.percentage_used?.toFixed(1)}%
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Claude Token Usage */}
        {tokenUsage && (
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-xs">
              <Zap className="w-3 h-3 text-gh-fg-muted" />
              <span className="text-gh-fg-default font-medium">Claude Tokens</span>
            </div>
            <div className="pl-4.5 space-y-0.5">
              {tokenStatus === 'error' ? (
                <div className="flex items-center gap-1 text-xs text-yellow-500">
                  <AlertTriangle className="w-3 h-3" />
                  <span>Token data unavailable</span>
                </div>
              ) : tokenStatus === 'no_index' || tokenStatus === 'empty' ? (
                <p className="text-xs text-gh-fg-muted">No token data recorded</p>
              ) : (
                <>
                  <TokenRow label="4 Hours" tokens={tokenUsage.tokens_4h} input={tokenUsage.input_tokens_4h} output={tokenUsage.output_tokens_4h} tasks={tokenUsage.task_count_4h} />
                  <TokenRow label="7 Days" tokens={tokenUsage.tokens_7d} input={tokenUsage.input_tokens_7d} output={tokenUsage.output_tokens_7d} tasks={tokenUsage.task_count_7d} />
                </>
              )}
            </div>
          </div>
        )}

        {/* Show error if data not available */}
        {!githubCheck?.api_usage && !tokenUsage && (
          <p className="text-xs text-gh-fg-muted">No usage data available</p>
        )}
      </div>
    </HeaderBox>
  )
}

function TokenRow({ label, tokens, input, output, tasks }) {
  return (
    <div className="flex items-center justify-between text-xs gap-2">
      <span className="text-gh-fg-muted">{label}</span>
      <span className="text-gh-fg-default" title={`Input: ${formatTokens(input)} / Output: ${formatTokens(output)}`}>
        {formatTokens(tokens)}
        {tasks > 0 && (
          <span className="text-gh-fg-muted ml-1">({tasks} {tasks === 1 ? 'task' : 'tasks'})</span>
        )}
      </span>
    </div>
  )
}
