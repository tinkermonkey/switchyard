import { useEffect, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { AlertCircle, TrendingUp, Clock, Play } from 'lucide-react'
import { useSocket } from '../contexts'

export default function FailureSignatureList() {
  const [signatures, setSignatures] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filters, setFilters] = useState({
    status: '',
    severity: '',
    investigation_status: '',
  })
  const [sortBy, setSortBy] = useState('last_seen')
  const [sortOrder, setSortOrder] = useState('desc')
  const { medicEvents } = useSocket()

  useEffect(() => {
    fetchSignatures()
  }, [filters, sortBy, sortOrder])

  // Refresh list when medic events occur
  useEffect(() => {
    if (medicEvents.length > 0) {
      fetchSignatures()
    }
  }, [medicEvents])

  const fetchSignatures = async () => {
    try {
      setLoading(true)
      const params = new URLSearchParams()
      if (filters.status) params.append('status', filters.status)
      if (filters.severity) params.append('severity', filters.severity)
      if (filters.investigation_status) params.append('investigation_status', filters.investigation_status)
      params.append('limit', '50')

      const response = await fetch(`/api/medic/failure-signatures?${params}`)
      if (!response.ok) throw new Error('Failed to fetch signatures')
      const data = await response.json()

      // Sort signatures
      const sorted = sortSignatures(data.signatures || [], sortBy, sortOrder)
      setSignatures(sorted)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const sortSignatures = (sigs, by, order) => {
    return [...sigs].sort((a, b) => {
      let aVal, bVal

      switch (by) {
        case 'last_seen':
          aVal = new Date(a.last_seen)
          bVal = new Date(b.last_seen)
          break
        case 'occurrence_count':
          aVal = a.occurrence_count
          bVal = b.occurrence_count
          break
        case 'severity':
          const severityOrder = { CRITICAL: 3, ERROR: 2, WARNING: 1 }
          aVal = severityOrder[a.severity] || 0
          bVal = severityOrder[b.severity] || 0
          break
        default:
          return 0
      }

      return order === 'asc' ? (aVal > bVal ? 1 : -1) : (aVal < bVal ? 1 : -1)
    })
  }

  const triggerInvestigation = async (fingerprintId) => {
    try {
      const response = await fetch(`/api/medic/investigations/${fingerprintId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ priority: 'high' })
      })
      if (!response.ok) throw new Error('Failed to trigger investigation')
      fetchSignatures() // Refresh list
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
        <div className="flex flex-wrap gap-4">
          <div>
            <label className="block text-xs text-gh-fg-muted mb-1">Status</label>
            <select
              value={filters.status}
              onChange={(e) => setFilters({...filters, status: e.target.value})}
              className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="">All</option>
              <option value="new">New</option>
              <option value="recurring">Recurring</option>
              <option value="trending">Trending</option>
              <option value="resolved">Resolved</option>
              <option value="ignored">Ignored</option>
            </select>
          </div>

          <div>
            <label className="block text-xs text-gh-fg-muted mb-1">Severity</label>
            <select
              value={filters.severity}
              onChange={(e) => setFilters({...filters, severity: e.target.value})}
              className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="">All</option>
              <option value="CRITICAL">Critical</option>
              <option value="ERROR">Error</option>
              <option value="WARNING">Warning</option>
            </select>
          </div>

          <div>
            <label className="block text-xs text-gh-fg-muted mb-1">Investigation</label>
            <select
              value={filters.investigation_status}
              onChange={(e) => setFilters({...filters, investigation_status: e.target.value})}
              className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="">All</option>
              <option value="not_started">Not Started</option>
              <option value="queued">Queued</option>
              <option value="in_progress">In Progress</option>
              <option value="completed">Completed</option>
            </select>
          </div>

          <div>
            <label className="block text-xs text-gh-fg-muted mb-1">Sort By</label>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="last_seen">Last Seen</option>
              <option value="occurrence_count">Occurrence Count</option>
              <option value="severity">Severity</option>
            </select>
          </div>

          <div>
            <label className="block text-xs text-gh-fg-muted mb-1">Order</label>
            <select
              value={sortOrder}
              onChange={(e) => setSortOrder(e.target.value)}
              className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="desc">Descending</option>
              <option value="asc">Ascending</option>
            </select>
          </div>
        </div>
      </div>

      {/* Signatures List */}
      {loading ? (
        <div className="flex items-center justify-center p-8">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
        </div>
      ) : error ? (
        <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
          <p className="text-sm text-red-500">Error: {error}</p>
        </div>
      ) : signatures.length === 0 ? (
        <div className="p-8 text-center bg-gh-canvas-subtle border border-gh-border rounded-lg">
          <p className="text-sm text-gh-fg-muted">No failure signatures found</p>
        </div>
      ) : (
        <div className="space-y-2">
          {signatures.map((sig) => (
            <FailureSignatureCard
              key={sig.fingerprint_id}
              signature={sig}
              onTriggerInvestigation={triggerInvestigation}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function FailureSignatureCard({ signature, onTriggerInvestigation }) {
  const getSeverityColor = (severity) => {
    switch (severity) {
      case 'CRITICAL': return 'text-red-500 bg-red-500/10 border-red-500/20'
      case 'ERROR': return 'text-orange-500 bg-orange-500/10 border-orange-500/20'
      case 'WARNING': return 'text-yellow-500 bg-yellow-500/10 border-yellow-500/20'
      default: return 'text-gray-500 bg-gray-500/10 border-gray-500/20'
    }
  }

  const getStatusColor = (status) => {
    switch (status) {
      case 'new': return 'text-blue-500 bg-blue-500/10'
      case 'recurring': return 'text-yellow-500 bg-yellow-500/10'
      case 'trending': return 'text-orange-500 bg-orange-500/10'
      case 'resolved': return 'text-green-500 bg-green-500/10'
      case 'ignored': return 'text-gray-500 bg-gray-500/10'
      default: return 'text-gray-500 bg-gray-500/10'
    }
  }

  const formatTimestamp = (timestamp) => {
    const date = new Date(timestamp)
    const now = new Date()
    const diff = now - date
    const minutes = Math.floor(diff / 60000)
    const hours = Math.floor(diff / 3600000)
    const days = Math.floor(diff / 86400000)

    if (minutes < 60) return `${minutes}m ago`
    if (hours < 24) return `${hours}h ago`
    return `${days}d ago`
  }

  return (
    <Link
      to={`/medic-detail/${signature.fingerprint_id}`}
      className="block bg-gh-canvas-subtle border border-gh-border rounded-lg p-4 hover:bg-gh-border-muted transition-colors"
    >
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${getSeverityColor(signature.severity)}`}>
              {signature.severity}
            </span>
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${getStatusColor(signature.status)}`}>
              {signature.status}
            </span>
            {signature.signature?.container_pattern && (
              <span className="px-2 py-0.5 rounded text-xs bg-gh-canvas border border-gh-border">
                {signature.signature.container_pattern}
              </span>
            )}
          </div>

          <div className="mb-2">
            <p className="text-sm font-medium text-gh-fg truncate">
              {signature.signature?.error_type}: {signature.signature?.normalized_message}
            </p>
            <p className="text-xs text-gh-fg-muted mt-1 font-mono truncate">
              {signature.signature?.error_pattern}
            </p>
          </div>

          <div className="flex items-center gap-4 text-xs text-gh-fg-muted">
            <div className="flex items-center gap-1">
              <AlertCircle className="w-3 h-3" />
              <span>{signature.occurrence_count} occurrences</span>
            </div>
            <div className="flex items-center gap-1">
              <TrendingUp className="w-3 h-3" />
              <span>{signature.occurrences_last_hour || 0} in last hour</span>
            </div>
            <div className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              <span>Last seen {formatTimestamp(signature.last_seen)}</span>
            </div>
          </div>
        </div>

        {(signature.investigation_status === 'not_started' || signature.investigation_status === 'failed') && (
          <button
            onClick={(e) => {
              e.preventDefault()
              onTriggerInvestigation(signature.fingerprint_id)
            }}
            className="ml-4 px-3 py-1.5 bg-gh-accent-emphasis text-white rounded text-xs hover:bg-gh-accent-primary transition-colors flex items-center gap-1"
          >
            <Play className="w-3 h-3" />
            {signature.investigation_status === 'failed' ? 'Retry' : 'Investigate'}
          </button>
        )}
        {signature.investigation_status !== 'not_started' && signature.investigation_status !== 'failed' && (
          <span className="ml-4 px-3 py-1.5 bg-blue-500/10 text-blue-500 rounded text-xs">
            {signature.investigation_status}
          </span>
        )}
      </div>
    </Link>
  )
}
