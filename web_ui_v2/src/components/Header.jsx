import { useState, useEffect } from 'react'
import { useSocket } from '../contexts/SocketContext'
import { useTheme } from '../contexts/ThemeContext'
import { Sun, Moon, AlertTriangle, CheckCircle, AlertCircle } from 'lucide-react'

export default function Header() {
  const { connected, stats } = useSocket()
  const { theme, toggleTheme } = useTheme()
  const [circuitBreakers, setCircuitBreakers] = useState([])
  const [cbSummary, setCbSummary] = useState({ open: 0, half_open: 0, healthy: 0 })

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

  return (
    <div className="space-y-3">
      {/* Circuit Breaker Alert Banner */}
      {(cbSummary.open > 0 || cbSummary.half_open > 0) && (
        <div className={`p-4 rounded-md border ${
          cbSummary.open > 0
            ? 'bg-red-50 dark:bg-red-900/20 border-gh-danger'
            : 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-500'
        }`}>
          <div className="flex items-start gap-3">
            <AlertTriangle className={`w-5 h-5 mt-0.5 ${
              cbSummary.open > 0 ? 'text-gh-danger' : 'text-yellow-600 dark:text-yellow-500'
            }`} />
            <div className="flex-1">
              <h3 className={`font-semibold mb-1 ${
                cbSummary.open > 0 ? 'text-gh-danger' : 'text-yellow-800 dark:text-yellow-300'
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
            <div className="flex gap-2">
              <span className={`inline-block px-3 py-1 rounded-full text-xs font-semibold ${
                connected
                  ? 'bg-gh-success text-white'
                  : 'bg-gh-danger text-white'
              }`}>
                {connected ? 'Connected' : 'Disconnected'}
              </span>

              {/* Circuit Breaker Badge */}
              {cbSummary.open === 0 && cbSummary.half_open === 0 && (
                <span className="inline-block px-3 py-1 rounded-full text-xs font-semibold bg-gh-success text-white">
                  All Systems Operational
                </span>
              )}
            </div>
          </div>

          {/* Right side: Stats cards */}
          <div className="flex gap-4 flex-wrap justify-end flex-1 mr-12">
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
