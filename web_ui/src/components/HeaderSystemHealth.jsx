/**
 * HeaderSystemHealth - Shows GitHub API and Claude token usage metrics
 */
import { Activity, Zap } from 'lucide-react'
import HeaderBox from './HeaderBox'
import { useSystemHealth } from '../hooks/useSystemHealth'

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
        {claudeCheck?.token_usage && (
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-xs">
              <Zap className="w-3 h-3 text-gh-fg-muted" />
              <span className="text-gh-fg-default font-medium">Claude Tokens</span>
            </div>
            <div className="pl-4.5 space-y-0.5">
              <div className="flex items-center justify-between text-xs">
                <span className="text-gh-fg-muted">4 Hours</span>
                <span className="text-gh-fg-default">
                  {formatNumber(claudeCheck.token_usage.tokens_4h)}
                </span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-gh-fg-muted">7 Days</span>
                <span className="text-gh-fg-default">
                  {formatNumber(claudeCheck.token_usage.tokens_7d)}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Show error if data not available */}
        {!githubCheck?.api_usage && !claudeCheck?.token_usage && (
          <p className="text-xs text-gh-fg-muted">No usage data available</p>
        )}
      </div>
    </HeaderBox>
  )
}

