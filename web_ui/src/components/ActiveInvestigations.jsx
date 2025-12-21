import { useEffect, useState } from 'react'
import { Search, Clock, Loader2, FileText } from 'lucide-react'
import { useSocket } from '../contexts'
import { Link } from '@tanstack/react-router'

export default function ActiveInvestigations() {
  const [activeInvestigations, setActiveInvestigations] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const { medicEvents } = useSocket()

  useEffect(() => {
    fetchActiveInvestigations()
    const interval = setInterval(fetchActiveInvestigations, 5000) // Refresh every 5 seconds
    return () => clearInterval(interval)
  }, [])

  // Refresh when medic events occur
  useEffect(() => {
    if (medicEvents.length > 0) {
      const latestEvent = medicEvents[0]
      if (latestEvent.event_type?.includes('investigation')) {
        fetchActiveInvestigations()
      }
    }
  }, [medicEvents])

  const fetchActiveInvestigations = async () => {
    try {
      const response = await fetch('/api/medic/investigations/active')
      if (!response.ok) throw new Error('Failed to fetch active investigations')
      const data = await response.json()
      setActiveInvestigations(data.investigations || [])
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const formatTimestamp = (isoString) => {
    if (!isoString) return 'Not started'

    const date = new Date(isoString)
    const now = new Date()
    const diffMs = now - date
    const diffMins = Math.floor(diffMs / 60000)

    if (diffMins < 1) return 'Just now'
    if (diffMins < 60) return `${diffMins}m ago`

    const diffHours = Math.floor(diffMins / 60)
    if (diffHours < 24) return `${diffHours}h ago`

    return date.toLocaleString()
  }

  const getStatusColor = (status) => {
    switch (status) {
      case 'in_progress':
      case 'starting':
        return 'text-blue-500 bg-blue-500/10 border-blue-500/20'
      case 'queued':
        return 'text-yellow-500 bg-yellow-500/10 border-yellow-500/20'
      default:
        return 'text-gray-500 bg-gray-500/10 border-gray-500/20'
    }
  }

  const getStatusLabel = (status) => {
    switch (status) {
      case 'in_progress':
        return 'Running'
      case 'starting':
        return 'Starting'
      case 'queued':
        return 'Queued'
      default:
        return status
    }
  }

  const getStatusIcon = (status) => {
    if (status === 'queued') {
      return <Clock className="w-4 h-4 text-yellow-500" />
    }
    return <Loader2 className="w-4 h-4 animate-spin text-blue-500" />
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8 bg-gh-canvas-subtle border border-gh-border rounded-lg">
        <div className="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-gh-accent-emphasis"></div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
        <p className="text-sm text-red-500">Error loading active investigations: {error}</p>
      </div>
    )
  }

  if (activeInvestigations.length === 0) {
    return (
      <div className="p-8 text-center bg-gh-canvas-subtle border border-gh-border rounded-lg">
        <Search className="w-12 h-12 mx-auto mb-3 text-gh-fg-muted" />
        <p className="text-gh-fg-muted">No active investigations</p>
      </div>
    )
  }

  return (
    <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-gh-canvas-subtle border-b border-gh-border">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-semibold text-gh-fg">Fingerprint</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-gh-fg">Status</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-gh-fg">Started</th>
              <th className="px-4 py-3 text-right text-xs font-semibold text-gh-fg">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gh-border">
            {activeInvestigations.map((investigation) => (
              <tr key={investigation.fingerprint_id} className="hover:bg-gh-canvas-inset">
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    {getStatusIcon(investigation.status)}
                    <Link
                      to={`/medic-detail/${investigation.fingerprint_id}`}
                      className="text-xs text-gh-accent-fg hover:underline font-mono"
                    >
                      {investigation.fingerprint_id.substring(0, 16)}...
                    </Link>
                  </div>
                </td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-1 rounded text-xs font-medium border ${getStatusColor(investigation.status)}`}>
                    {getStatusLabel(investigation.status)}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-1 text-sm text-gh-fg-muted">
                    <Clock className="w-3 h-3" />
                    {formatTimestamp(investigation.started_at)}
                  </div>
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center justify-end gap-2">
                    <Link
                      to={`/medic-detail/${investigation.fingerprint_id}`}
                      className="px-2 py-1 text-xs text-blue-500 hover:text-blue-400 hover:bg-blue-500/10 rounded transition-colors flex items-center gap-1"
                      title="View details"
                    >
                      <FileText className="w-3 h-3" />
                      Details
                    </Link>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
