/**
 * HeaderSystemHealth - Shows individual health check statuses
 */
import { CheckCircle, XCircle, AlertTriangle } from 'lucide-react'
import HeaderBox from './HeaderBox'
import { useSystemHealth } from '../hooks/useSystemHealth'

export default function HeaderSystemHealth() {
  const { checks, loading } = useSystemHealth()

  if (loading || !checks) {
    return (
      <HeaderBox title="System Health" minWidth="min-w-[200px]">
        <p className="text-xs text-gh-fg-muted">Loading...</p>
      </HeaderBox>
    )
  }

  // Priority order of checks to display
  const priorityChecks = ['github', 'claude', 'disk', 'memory']
  
  const getHealthIcon = (check) => {
    if (!check) return <AlertTriangle className="w-3 h-3 text-gh-fg-muted" />
    if (check.healthy) return <CheckCircle className="w-3 h-3 text-gh-success" />
    return <XCircle className="w-3 h-3 text-gh-danger" />
  }

  const getHealthLabel = (key) => {
    const labels = {
      'github': 'GitHub',
      'claude': 'Claude',
      'disk': 'Disk',
      'memory': 'Memory'
    }
    return labels[key] || key
  }

  return (
    <HeaderBox title="System Health" minWidth="min-w-[200px]">
      <div className="space-y-1">
        {priorityChecks.map((key) => {
          const check = checks[key]
          if (!check) return null
          
          return (
            <div key={key} className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-1.5">
                {getHealthIcon(check)}
                <span className="text-gh-fg-default">{getHealthLabel(key)}</span>
              </div>
              <span className={check.healthy ? 'text-gh-success' : 'text-gh-danger'}>
                {check.healthy ? 'OK' : 'Error'}
              </span>
            </div>
          )
        })}
      </div>
    </HeaderBox>
  )
}
