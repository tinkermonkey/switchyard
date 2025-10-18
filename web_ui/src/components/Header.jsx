import { useSocket } from '../contexts/SocketContext'
import { useTheme } from '../contexts/ThemeContext'
import { useSystemHealth } from '../hooks/useSystemHealth'
import { useCircuitBreakers } from '../hooks/useCircuitBreakers'
import { Sun, Moon, AlertTriangle, AlertCircle } from 'lucide-react'
import HeaderActiveAgents from './HeaderActiveAgents'
import HeaderSystemHealth from './HeaderSystemHealth'
import HeaderCircuitBreakers from './HeaderCircuitBreakers'
import HeaderClaudeUsage from './HeaderClaudeUsage'
import HeaderStatsCard from './HeaderStatsCard'

export default function Header() {
  const { connected, stats } = useSocket()
  const { theme, toggleTheme } = useTheme()
  
  // Use centralized state hooks
  const { 
    systemHealth, 
    unhealthyComponents, 
    isHealthy, 
    isDegraded, 
    isUnhealthy, 
    isStarting,
    status: healthStatus 
  } = useSystemHealth()
  
  const { 
    circuitBreakers, 
    summary: cbSummary, 
    hasOpenBreakers, 
    hasHalfOpenBreakers,
    problematicBreakers 
  } = useCircuitBreakers()

  // Helper for display in alert banners
  const getStateIcon = (state) => {
    switch (state) {
      case 'half_open':
        return <AlertCircle className="w-4 h-4" />
      case 'open':
        return <AlertTriangle className="w-4 h-4" />
      default:
        return null
    }
  }
  
  const getStateColor = (state) => {
    switch (state) {
      case 'closed':
        return 'text-gh-success'
      case 'half_open':
        return 'text-yellow-500'
      case 'open':
        return 'text-gh-danger'
      default:
        return 'text-gh-fg-muted'
    }
  }

  const getHealthComponentName = (key) => {
    const names = {
      'github': 'GitHub API',
      'claude': 'Claude Code CLI',
      'disk': 'Disk Space',
      'memory': 'Memory',
      'claude_usage': 'Claude Code Usage'
    }
    return names[key] || key
  }

  const getHealthComponentDetails = (key, check) => {
    if (!check) return null

    switch (key) {
      case 'github':
        if (!check.healthy) {
          return check.error || 'GitHub connectivity issue'
        }
        return `Connected to ${check.tested_org || 'GitHub'} • Projects: ${check.projects_access || 'unknown'}`

      case 'disk':
        return check.healthy
          ? `${check.free_gb?.toFixed(1)}GB free (${check.usage_percent?.toFixed(1)}% used)`
          : `Low disk space: ${check.usage_percent?.toFixed(1)}% used`

      case 'memory':
        return check.healthy
          ? `${check.available_gb?.toFixed(1)}GB available (${check.usage_percent?.toFixed(1)}% used)`
          : `High memory usage: ${check.usage_percent?.toFixed(1)}%`

      case 'claude':
        return check.healthy ? 'Available' : 'Not accessible'

      case 'claude_usage': {
        if (!check.available) {
          return check.error || 'Usage data unavailable'
        }

        const formatTokens = (tokens) => {
          if (!tokens) return '0'
          const millions = tokens / 1000000
          return millions >= 1000 ? `${(millions / 1000).toFixed(2)}B` : `${millions.toFixed(1)}M`
        }

        // Build quota information sections
        const sections = []

        if (check.weekly_quota) {
          const weeklyUsed = formatTokens(check.weekly_usage)
          const weeklyQuota = formatTokens(check.weekly_quota)
          const weeklyPercent = check.weekly_usage_percent || 0
          sections.push(`Weekly: ${weeklyUsed}/${weeklyQuota} (${weeklyPercent.toFixed(1)}%)`)
        }

        if (check.session_quota) {
          const sessionUsed = formatTokens(check.session_usage)
          const sessionQuota = formatTokens(check.session_quota)
          const sessionPercent = check.session_usage_percent || 0
          const remainingMins = check.session_remaining_minutes || 0
          sections.push(`Session: ${sessionUsed}/${sessionQuota} (${sessionPercent.toFixed(1)}%, ${remainingMins}m left)`)
        }

        if (sections.length === 0) {
          const todayTokens = formatTokens(check.last_day_tokens)
          const todayCost = check.last_day_cost ? `$${check.last_day_cost.toFixed(2)}` : '$0.00'
          return `Today: ${todayCost} (${todayTokens} tokens)`
        }

        return sections.join(' • ')
      }

      default:
        return check.healthy ? 'OK' : (check.error || 'Failed')
    }
  }

  return (
    <div className="space-y-3">
      {/* System Health Alert Banner */}
      {systemHealth && (healthStatus === 'unhealthy' || healthStatus === 'error' || healthStatus === 'starting' || (healthStatus === 'degraded' && unhealthyComponents.length > 0)) && (
        <div className={`p-4 rounded-md border ${healthStatus === 'error' || healthStatus === 'unhealthy'
          ? 'bg-red-50 dark:bg-red-900/20 border-gh-danger'
          : 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-500'
          }`}>
          <div className="flex items-start gap-3">
            <AlertTriangle className={`w-5 h-5 mt-0.5 ${healthStatus === 'error' || healthStatus === 'unhealthy'
              ? 'text-gh-danger'
              : 'text-yellow-600 dark:text-yellow-500'
              }`} />
            <div className="flex-1">
              <h3 className={`font-semibold mb-1 ${healthStatus === 'error' || healthStatus === 'unhealthy'
                ? 'text-gh-danger'
                : 'text-yellow-800 dark:text-yellow-300'
                }`}>
                {healthStatus === 'starting' && 'System Starting Up'}
                {healthStatus === 'error' && 'Health Check Error'}
                {healthStatus === 'unhealthy' && 'System Health Issues Detected'}
                {healthStatus === 'degraded' && 'System Running with Reduced Functionality'}
              </h3>
              <p className="text-sm text-gh-fg-default mb-2">
                {healthStatus === 'starting' && 'Orchestrator is initializing, health checks not yet complete.'}
                {healthStatus === 'error' && (systemHealth.message || systemHealth.error || 'Unable to retrieve system health status.')}
                {healthStatus === 'unhealthy' && `${unhealthyComponents.length} component${unhealthyComponents.length > 1 ? 's are' : ' is'} reporting issues.`}
                {healthStatus === 'degraded' && 'Core functionality is operational, but some features are unavailable due to authentication configuration.'}
              </p>
              {unhealthyComponents.length > 0 && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {unhealthyComponents.map(([key, check]) => (
                    <div key={key} className="bg-white dark:bg-gh-canvas p-2 rounded border border-gh-border text-sm">
                      <div className="flex items-center gap-2 mb-1">
                        <AlertTriangle className="w-4 h-4 text-gh-danger" />
                        <span className="font-semibold text-gh-fg-default">{getHealthComponentName(key)}</span>
                      </div>
                      <div className="text-xs text-gh-fg-muted">
                        {getHealthComponentDetails(key, check)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {systemHealth.status === 'degraded' && systemHealth.orchestrator?.checks?.github?.warnings && (
                <div className="mt-2 space-y-1">
                  {systemHealth.orchestrator.checks.github.warnings.map((warning, idx) => (
                    <div key={idx} className="text-sm text-yellow-800 dark:text-yellow-300 flex items-start gap-2">
                      <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                      <span>{warning}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Circuit Breaker Alert Banner */}
      {(hasOpenBreakers || hasHalfOpenBreakers) && (
        <div className={`p-4 rounded-md border ${hasOpenBreakers
          ? 'bg-red-50 dark:bg-red-900/20 border-gh-danger'
          : 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-500'
          }`}>
          <div className="flex items-start gap-3">
            <AlertTriangle className={`w-5 h-5 mt-0.5 ${hasOpenBreakers ? 'text-gh-danger' : 'text-yellow-600 dark:text-yellow-500'
              }`} />
            <div className="flex-1">
              <h3 className={`font-semibold mb-1 ${hasOpenBreakers ? 'text-gh-danger' : 'text-yellow-800 dark:text-yellow-300'
                }`}>
                {hasOpenBreakers ? 'Circuit Breakers Open' : 'Circuit Breakers Testing Recovery'}
              </h3>
              <p className="text-sm text-gh-fg-default mb-2">
                {hasOpenBreakers
                  ? `${cbSummary.open} circuit breaker${cbSummary.open > 1 ? 's are' : ' is'} open. Services are failing and requests are being rejected.`
                  : `${cbSummary.half_open} circuit breaker${cbSummary.half_open > 1 ? 's are' : ' is'} testing recovery.`
                }
              </p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                {problematicBreakers.map((cb, idx) => (
                    <div key={idx} className="bg-white dark:bg-gh-canvas p-2 rounded border border-gh-border text-sm">
                      <div className="flex items-center gap-2 mb-1">
                        <span className={getStateColor(cb.state)}>
                          {getStateIcon(cb.state)}
                        </span>
                        <span className="font-semibold text-gh-fg-default">{cb.name}</span>
                      </div>
                      <div className="text-xs text-gh-fg-muted">
                        State: <span className={`font-medium ${getStateColor(cb.state)}`}>
                          {cb.state.replace('_', ' ').toUpperCase()}
                        </span>
                        {cb.state === 'open' && ` • Rejected: ${cb.total_rejected}`}
                        {cb.state === 'half_open' && ` • Testing...`}
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Main Header */}
      <div className="bg-gh-canvas-subtle p-5 rounded-md border border-gh-border relative">
        {/* Theme toggle - upper right corner */}
        <button
          onClick={toggleTheme}
          className="absolute top-5 right-5 p-2 bg-gh-canvas border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
        </button>

        {/* Main content */}
        <div className="flex items-start justify-between gap-6">
          {/* Left side: Title and badges */}
          <div className="flex-shrink-0">
            <h1 className="text-gh-accent-primary text-2xl font-semibold mb-3">
              Agent Observability Dashboard
            </h1>
            <div className="flex gap-2 flex-wrap">
              <span className={`inline-block px-3 py-1 rounded-full text-xs font-semibold ${connected
                ? 'bg-gh-success text-white'
                : 'bg-gh-danger text-white'
                }`}>
                {connected ? 'WebSocket Connected' : 'WebSocket Disconnected'}
              </span>
            </div>
          </div>

          {/* Right side: Stats cards */}
          <div className="flex gap-4 flex-wrap justify-end flex-1 mr-12">
            {/* Only show these blocks if connected */}
            {connected && (
              <>
                <HeaderActiveAgents />
                <HeaderClaudeUsage />
                <HeaderSystemHealth />
                <HeaderCircuitBreakers />
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
