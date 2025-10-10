/**
 * HeaderClaudeUsage - Shows Claude usage quotas with progress bars
 */
import HeaderBox from './HeaderBox'
import { useSystemHealth } from '../hooks/useSystemHealth'

export default function HeaderClaudeUsage() {
  const { systemHealth } = useSystemHealth()
  
  const usage = systemHealth?.orchestrator?.checks?.claude_usage

  if (!usage?.available) {
    return null
  }

  const formatTokens = (tokens) => {
    if (!tokens) return '0'
    const millions = tokens / 1000000
    return millions >= 1000 ? `${(millions / 1000).toFixed(1)}B` : `${millions.toFixed(0)}M`
  }

  const getQuotaColor = (percent) => {
    if (percent >= 90) return 'bg-gh-danger'
    if (percent >= 75) return 'bg-yellow-500'
    return 'bg-gh-success'
  }

  const weeklyPercent = usage.weekly_usage_percent || 0
  const sessionPercent = usage.session_usage_percent || 0

  return (
    <HeaderBox title="Claude Usage" minWidth="min-w-[180px]">
      <div className="space-y-2">
        {usage.weekly_quota && (
          <div>
            <div className="flex justify-between items-center text-xs mb-0.5">
              <span className="text-gh-fg-muted">Weekly</span>
              <span className="font-semibold text-gh-fg-default">
                {formatTokens(usage.weekly_usage)}/{formatTokens(usage.weekly_quota)}
              </span>
            </div>
            <div className="w-full bg-gh-border-muted rounded-full h-1.5">
              <div
                className={`h-1.5 rounded-full transition-all ${getQuotaColor(weeklyPercent)}`}
                style={{ width: `${Math.min(weeklyPercent, 100)}%` }}
              />
            </div>
          </div>
        )}
        {usage.session_quota && (
          <div>
            <div className="flex justify-between items-center text-xs mb-0.5">
              <span className="text-gh-fg-muted">Session ({usage.session_remaining_minutes || 0}m)</span>
              <span className="font-semibold text-gh-fg-default">
                {formatTokens(usage.session_usage)}/{formatTokens(usage.session_quota)}
              </span>
            </div>
            <div className="w-full bg-gh-border-muted rounded-full h-1.5">
              <div
                className={`h-1.5 rounded-full transition-all ${getQuotaColor(sessionPercent)}`}
                style={{ width: `${Math.min(sessionPercent, 100)}%` }}
              />
            </div>
          </div>
        )}
      </div>
    </HeaderBox>
  )
}
