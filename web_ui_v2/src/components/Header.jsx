import { useState, useEffect } from 'react'
import { useSocket } from '../contexts/SocketContext'
import { useTheme } from '../contexts/ThemeContext'
import { Sun, Moon, AlertTriangle, CheckCircle, AlertCircle } from 'lucide-react'

export default function Header() {
  const { connected, stats } = useSocket()
  const { theme, toggleTheme } = useTheme()
  const [circuitBreakers, setCircuitBreakers] = useState([])
  const [cbSummary, setCbSummary] = useState({ open: 0, half_open: 0, healthy: 0 })
  const [systemHealth, setSystemHealth] = useState(null)

  // Fetch circuit breaker status
  useEffect(() => {
    const fetchCircuitBreakers = async () => {
      try {
        const response = await fetch('/api/circuit-breakers')
        const data = await response.json()

        if (data.success) {
          setCircuitBreakers(data.circuit_breakers)
          setCbSummary(data.summary)
        }
      } catch (error) {
        console.error('Error fetching circuit breakers:', error)
      }
    }

    // Fetch immediately
    fetchCircuitBreakers()

    // Poll every 5 seconds
    const interval = setInterval(fetchCircuitBreakers, 5000)
    return () => clearInterval(interval)
  }, [])

  // Fetch system health status
  useEffect(() => {
    const fetchHealth = async () => {
      try {
        const response = await fetch('/health')
        const data = await response.json()
        setSystemHealth(data)
      } catch (error) {
        console.error('Error fetching health status:', error)
        setSystemHealth({ status: 'error', error: error.message })
      }
    }

    // Fetch immediately
    fetchHealth()

    // Poll every 10 seconds
    const interval = setInterval(fetchHealth, 10000)
    return () => clearInterval(interval)
  }, [])

  const statCards = [
    { title: 'Total Events', value: stats.totalEvents },
    { title: 'Active Tasks', value: stats.activeTasks },
    { title: 'Total Tokens', value: stats.totalTokens.toLocaleString() },
    { title: 'Avg API Latency', value: `${stats.avgLatency}ms` },
  ]

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

  const getStateIcon = (state) => {
    switch (state) {
      case 'closed':
        return <CheckCircle className="w-4 h-4" />
      case 'half_open':
        return <AlertCircle className="w-4 h-4" />
      case 'open':
        return <AlertTriangle className="w-4 h-4" />
      default:
        return null
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

  const unhealthyComponents = systemHealth?.orchestrator?.checks
    ? Object.entries(systemHealth.orchestrator.checks).filter(([, check]) => !check.healthy)
    : []

  return (
    <div className="space-y-3">
      {/* System Health Alert Banner */}
      {systemHealth && (systemHealth.status === 'unhealthy' || systemHealth.status === 'starting' || systemHealth.status === 'error') && (
        <div className={`p-4 rounded-md border ${systemHealth.status === 'error' || systemHealth.status === 'unhealthy'
            ? 'bg-red-50 dark:bg-red-900/20 border-gh-danger'
            : 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-500'
          }`}>
          <div className="flex items-start gap-3">
            <AlertTriangle className={`w-5 h-5 mt-0.5 ${systemHealth.status === 'error' || systemHealth.status === 'unhealthy'
                ? 'text-gh-danger'
                : 'text-yellow-600 dark:text-yellow-500'
              }`} />
            <div className="flex-1">
              <h3 className={`font-semibold mb-1 ${systemHealth.status === 'error' || systemHealth.status === 'unhealthy'
                  ? 'text-gh-danger'
                  : 'text-yellow-800 dark:text-yellow-300'
                }`}>
                {systemHealth.status === 'starting' && 'System Starting Up'}
                {systemHealth.status === 'error' && 'Health Check Error'}
                {systemHealth.status === 'unhealthy' && 'System Health Issues Detected'}
              </h3>
              <p className="text-sm text-gh-fg-default mb-2">
                {systemHealth.status === 'starting' && 'Orchestrator is initializing, health checks not yet complete.'}
                {systemHealth.status === 'error' && (systemHealth.message || systemHealth.error || 'Unable to retrieve system health status.')}
                {systemHealth.status === 'unhealthy' && `${unhealthyComponents.length} component${unhealthyComponents.length > 1 ? 's are' : ' is'} reporting issues.`}
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
            </div>
          </div>
        </div>
      )}

      {/* Circuit Breaker Alert Banner */}
      {(cbSummary.open > 0 || cbSummary.half_open > 0) && (
        <div className={`p-4 rounded-md border ${cbSummary.open > 0
            ? 'bg-red-50 dark:bg-red-900/20 border-gh-danger'
            : 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-500'
          }`}>
          <div className="flex items-start gap-3">
            <AlertTriangle className={`w-5 h-5 mt-0.5 ${cbSummary.open > 0 ? 'text-gh-danger' : 'text-yellow-600 dark:text-yellow-500'
              }`} />
            <div className="flex-1">
              <h3 className={`font-semibold mb-1 ${cbSummary.open > 0 ? 'text-gh-danger' : 'text-yellow-800 dark:text-yellow-300'
                }`}>
                {cbSummary.open > 0 ? 'Circuit Breakers Open' : 'Circuit Breakers Testing Recovery'}
              </h3>
              <p className="text-sm text-gh-fg-default mb-2">
                {cbSummary.open > 0
                  ? `${cbSummary.open} circuit breaker${cbSummary.open > 1 ? 's are' : ' is'} open. Services are failing and requests are being rejected.`
                  : `${cbSummary.half_open} circuit breaker${cbSummary.half_open > 1 ? 's are' : ' is'} testing recovery.`
                }
              </p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                {circuitBreakers
                  .filter(cb => cb.state !== 'closed')
                  .map((cb, idx) => (
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
                {connected ? 'Connected' : 'Disconnected'}
              </span>

              {/* System Health Badge */}
              {systemHealth && (
                <span className={`inline-block px-3 py-1 rounded-full text-xs font-semibold ${systemHealth.status === 'healthy'
                    ? 'bg-gh-success text-white'
                    : systemHealth.status === 'starting'
                      ? 'bg-yellow-500 text-white'
                      : 'bg-gh-danger text-white'
                  }`}>
                  {systemHealth.status === 'healthy' && 'System Healthy'}
                  {systemHealth.status === 'starting' && 'Starting...'}
                  {(systemHealth.status === 'unhealthy' || systemHealth.status === 'error') && `${unhealthyComponents.length} Component${unhealthyComponents.length > 1 ? 's' : ''} Down`}
                </span>
              )}

              {/* Circuit Breaker Badge */}
              {cbSummary.open === 0 && cbSummary.half_open === 0 && (
                <span className="inline-block px-3 py-1 rounded-full text-xs font-semibold bg-gh-success text-white">
                  All Breakers Closed
                </span>
              )}


            </div>
          </div>

          {/* Right side: Stats cards */}
          <div className="flex gap-4 flex-wrap justify-end flex-1 mr-12">
            {/* Claude Usage with Progress Bars */}
            {systemHealth?.orchestrator?.checks?.claude_usage?.available && (() => {
              const usage = systemHealth.orchestrator.checks.claude_usage
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
                <div className="bg-gh-canvas p-3 rounded-md border border-gh-border min-w-[140px]">
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
              )
            })()}
            {statCards.map((card, idx) => (
              <div key={idx} className="bg-gh-canvas p-3 rounded-md border border-gh-border min-w-[140px]">
                <h3 className="text-gh-fg-muted text-xs uppercase mb-1">
                  {card.title}
                </h3>
                <div className="text-xl font-semibold text-gh-accent-primary">
                  {card.value}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
