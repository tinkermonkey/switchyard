/**
 * HeaderCircuitBreakers - Shows individual circuit breaker states
 */
import { useState } from 'react'
import { CheckCircle, XCircle, AlertCircle, RotateCcw } from 'lucide-react'
import HeaderBox from './HeaderBox'
import { useCircuitBreakers } from '../hooks/useCircuitBreakers'

export default function HeaderCircuitBreakers() {
  const { circuitBreakers, loading, reset } = useCircuitBreakers()
  const maxDisplayedBreakers = 5
  const [resetting, setResetting] = useState({})

  if (loading) {
    return (
      <HeaderBox title="Circuit Breakers" minWidth="md:min-w-[180px]">
        <p className="text-xs text-gh-fg-muted">Loading...</p>
      </HeaderBox>
    )
  }

  const handleReset = async (cb, idx) => {
    // Set loading state for this specific breaker
    setResetting(prev => ({ ...prev, [idx]: true }))
    
    try {
      const result = await reset(cb)
      if (!result.success) {
        console.error('Failed to reset circuit breaker:', result.error)
        // Could show a toast notification here
      }
    } catch (error) {
      console.error('Error resetting circuit breaker:', error)
    } finally {
      setResetting(prev => ({ ...prev, [idx]: false }))
    }
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

  const canReset = (cb) => {
    return cb.state === 'open' || cb.state === 'half_open'
  }

  return (
    <HeaderBox title="Circuit Breakers" minWidth="md:min-w-[180px]">
      <div className="space-y-1">
        {circuitBreakers.length === 0 ? (
          <p className="text-xs text-gh-fg-muted">No breakers</p>
        ) : (
          circuitBreakers.slice(0, maxDisplayedBreakers).map((cb, idx) => (
            <div key={idx}>
              <div className="flex items-center justify-between text-xs">
                <div className="flex items-center gap-1.5 flex-1 min-w-0">
                  {getStateIcon(cb.state)}
                  <span className="text-gh-fg-default truncate max-w-[100px]" title={cb.name}>
                    {cb.name}
                  </span>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className={getStateColor(cb.state)}>
                    {getStateLabel(cb.state)}
                  </span>
                  {canReset(cb) && (
                    <button
                      onClick={() => handleReset(cb, idx)}
                      disabled={resetting[idx]}
                      className="p-0.5 hover:bg-gh-canvas-subtle rounded transition-colors disabled:opacity-50"
                      title="Reset circuit breaker"
                    >
                      <RotateCcw className={`w-3 h-3 text-gh-fg-muted hover:text-gh-fg-default ${resetting[idx] ? 'animate-spin' : ''}`} />
                    </button>
                  )}
                </div>
              </div>
              {cb.rate_limit && (
                <div className="text-xs text-gh-fg-muted ml-5">
                  API: {cb.rate_limit.remaining}/{cb.rate_limit.limit} ({cb.rate_limit.percentage_used.toFixed(0)}%)
                </div>
              )}
            </div>
          ))
        )}
        {circuitBreakers.length > maxDisplayedBreakers && (
          <p className="text-xs text-gh-fg-muted italic">
            +{circuitBreakers.length - maxDisplayedBreakers} more
          </p>
        )}
      </div>
    </HeaderBox>
  )
}

