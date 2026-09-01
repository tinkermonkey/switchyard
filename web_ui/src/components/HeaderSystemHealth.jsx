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

// "2026-09-01T10:52:48.159015" -> "3m ago". Used to make a stale GitHub
// rate-limit reading self-evidently stale (an age, not just a dim asterisk) -
// staleness is the expected state whenever nothing has called GitHub
// recently, not a bug signal, so it needs to read as normal, not alarming.
const formatRelativeTime = (isoString) => {
  if (!isoString) return null
  const then = new Date(isoString).getTime()
  if (Number.isNaN(then)) return null
  const diffSeconds = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (diffSeconds < 60) return 'just now'
  const diffMinutes = Math.floor(diffSeconds / 60)
  if (diffMinutes < 60) return `${diffMinutes}m ago`
  const diffHours = Math.floor(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  return `${Math.floor(diffHours / 24)}d ago`
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
        {/* GitHub API Usage - GraphQL and REST are separate rate-limit
            buckets tracked from real per-call response headers, shared
            across every process using the token via Redis (issue #103
            follow-up); fall back to the conflated 'api_usage' field if a
            health payload without the split fields is ever seen. */}
        {(githubCheck?.api_usage_graphql || githubCheck?.api_usage_rest || githubCheck?.api_usage) && (
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-xs">
              <Activity className="w-3 h-3 text-gh-fg-muted" />
              <span className="text-gh-fg-default font-medium">GitHub API</span>
            </div>
            <div className="pl-4.5 space-y-0.5">
              {githubCheck?.api_usage_graphql || githubCheck?.api_usage_rest ? (
                <>
                  <GithubBucketRow
                    label="GraphQL"
                    bucket={githubCheck?.api_usage_graphql}
                    getUsageColor={getUsageColor}
                    formatNumber={formatNumber}
                  />
                  <GithubBucketRow
                    label="REST"
                    bucket={githubCheck?.api_usage_rest}
                    getUsageColor={getUsageColor}
                    formatNumber={formatNumber}
                  />
                </>
              ) : (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gh-fg-muted">Remaining</span>
                  <span className={getUsageColor(githubCheck.api_usage.percentage_used)}>
                    {formatNumber(githubCheck.api_usage.remaining)} / {formatNumber(githubCheck.api_usage.limit)}
                  </span>
                </div>
              )}
              {/* Call volume doesn't depend on the token-quota mechanism
                  above at all, so it stays meaningful context even while
                  the quota reading is stale or never-observed - a way to
                  see "the system is working" independent of that number. */}
              {githubCheck?.api_call_stats && (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gh-fg-muted">Calls (since restart)</span>
                  <span className="text-gh-fg-default">
                    {formatNumber(githubCheck.api_call_stats.total_requests)}
                  </span>
                </div>
              )}
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

// Renders one rate-limit bucket in one of three distinct states:
// never observed (no process has ever published a real reading for this
// bucket), stale (have a reading, but nothing's called GitHub recently
// enough to trust it), or fresh. Conflating the first two into a single
// "stale" flag used to make a genuinely broken feature look identical to
// a quiet, healthy one (issue #103) - keeping them apart is the point.
function GithubBucketRow({ label, bucket, getUsageColor, formatNumber }) {
  if (!bucket) return null

  if (bucket.never_observed) {
    return (
      <div className="flex items-center justify-between text-xs">
        <span className="text-gh-fg-muted">{label}</span>
        <span className="text-gh-fg-muted" title="No real GitHub API response has been observed yet">
          not yet observed
        </span>
      </div>
    )
  }

  const age = formatRelativeTime(bucket.last_updated)

  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-gh-fg-muted">{label}</span>
      <span className="text-right">
        <span className={getUsageColor(bucket.percentage_used)}>
          {formatNumber(bucket.remaining)} / {formatNumber(bucket.limit)}
        </span>
        {bucket.stale && age && (
          <span
            className="text-gh-fg-muted ml-1"
            title="No recent GitHub call to refresh this number"
          >
            ({age})
          </span>
        )}
      </span>
    </div>
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
