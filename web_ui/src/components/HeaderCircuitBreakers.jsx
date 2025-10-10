/**
 * HeaderCircuitBreakers - Shows individual circuit breaker states
 */
import { CheckCircle, XCircle, AlertCircle } from 'lucide-react'
import HeaderBox from './HeaderBox'
import { useCircuitBreakers } from '../hooks/useCircuitBreakers'

export default function HeaderCircuitBreakers() {
  const { circuitBreakers, loading } = useCircuitBreakers()

  if (loading) {
    return (
      <HeaderBox title="Circuit Breakers" minWidth="min-w-[180px]">
        <p className="text-xs text-gh-fg-muted">Loading...</p>
      </HeaderBox>
    )
  }

  const getStateIcon = (state) => {
    switch (state) {
      case 'closed':
        return <CheckCircle className="w-3 h-3 text-gh-success" />
      case 'half_open':
        return <AlertCircle className="w-3 h-3 text-yellow-500" />
      case 'open':
        return <XCircle className="w-3 h-3 text-gh-danger" />
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

  const getStateLabel = (state) => {
    switch (state) {
      case 'closed':
        return 'Closed'
      case 'half_open':
        return 'Half Open'
      case 'open':
        return 'Open'
      default:
        return state
    }
  }

  return (
    <HeaderBox title="Circuit Breakers" minWidth="min-w-[180px]">
      <div className="space-y-1">
        {circuitBreakers.length === 0 ? (
          <p className="text-xs text-gh-fg-muted">No breakers</p>
        ) : (
          circuitBreakers.slice(0, 4).map((cb, idx) => (
            <div key={idx} className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-1.5">
                {getStateIcon(cb.state)}
                <span className="text-gh-fg-default truncate max-w-[100px]" title={cb.name}>
                  {cb.name}
                </span>
              </div>
              <span className={getStateColor(cb.state)}>
                {getStateLabel(cb.state)}
              </span>
            </div>
          ))
        )}
        {circuitBreakers.length > 4 && (
          <p className="text-xs text-gh-fg-muted italic">
            +{circuitBreakers.length - 4} more
          </p>
        )}
      </div>
    </HeaderBox>
  )
}
