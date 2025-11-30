import { useEffect, useState } from 'react'
import { AlertCircle, Clock, Code, Play, FileText, Wrench } from 'lucide-react'
import InvestigationReport from './InvestigationReport'
import { useSocket } from '../contexts'

export default function FailureSignatureDetail({ fingerprintId }) {
  const [signature, setSignature] = useState(null)
  const [occurrences, setOccurrences] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showDiagnosis, setShowDiagnosis] = useState(false)
  const [showFixPlan, setShowFixPlan] = useState(false)
  const { medicEvents } = useSocket()

  useEffect(() => {
    fetchSignatureDetails()
    fetchOccurrences()
  }, [fingerprintId])

  // Refresh when medic events occur for this signature
  useEffect(() => {
    if (medicEvents.length > 0) {
      const latestEvent = medicEvents[0]
      // Check if event is related to this signature
      // Event can have fingerprint_id in data.fingerprint_id or task_id
      const eventFingerprintId = latestEvent.data?.fingerprint_id || latestEvent.task_id
      if (eventFingerprintId === fingerprintId) {
        console.log('[FailureSignatureDetail] Refreshing due to event:', latestEvent.event_type)
        fetchSignatureDetails()
        // Also refresh occurrences if it's a new occurrence
        if (latestEvent.event_type === 'signature_updated') {
          fetchOccurrences()
        }
      }
    }
  }, [medicEvents, fingerprintId])

  const fetchSignatureDetails = async () => {
    try {
      const response = await fetch(`/api/medic/failure-signatures/${fingerprintId}`)
      if (!response.ok) throw new Error('Failed to fetch signature details')
      const data = await response.json()
      setSignature(data)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const fetchOccurrences = async () => {
    try {
      const response = await fetch(`/api/medic/failure-signatures/${fingerprintId}/occurrences?limit=50`)
      if (!response.ok) throw new Error('Failed to fetch occurrences')
      const data = await response.json()
      setOccurrences(data.occurrences || [])
    } catch (err) {
      console.error('Error fetching occurrences:', err)
    }
  }

  const triggerInvestigation = async () => {
    try {
      const response = await fetch(`/api/medic/investigations/${fingerprintId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ priority: 'high' })
      })
      if (!response.ok) throw new Error('Failed to trigger investigation')
      fetchSignatureDetails() // Refresh to update investigation status
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  const triggerFix = async () => {
    try {
      const response = await fetch(`/api/medic/claude/fixes/${fingerprintId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      })
      if (!response.ok) throw new Error('Failed to trigger fix')
      fetchSignatureDetails() // Refresh
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  const updateStatus = async (newStatus) => {
    try {
      const response = await fetch(`/api/medic/failure-signatures/${fingerprintId}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus })
      })
      if (!response.ok) throw new Error('Failed to update status')
      fetchSignatureDetails()
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
        <p className="text-sm text-red-500">Error: {error}</p>
      </div>
    )
  }

  if (!signature) return null

  const getSeverityColor = (severity) => {
    switch (severity) {
      case 'CRITICAL': return 'text-red-500 bg-red-500/10 border-red-500/20'
      case 'ERROR': return 'text-orange-500 bg-orange-500/10 border-orange-500/20'
      case 'WARNING': return 'text-yellow-500 bg-yellow-500/10 border-yellow-500/20'
      default: return 'text-gray-500 bg-gray-500/10 border-gray-500/20'
    }
  }

  const formatTimestamp = (timestamp) => {
    return new Date(timestamp).toLocaleString()
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-6">
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className={`px-3 py-1 rounded text-sm font-medium ${getSeverityColor(signature.severity)}`}>
                {signature.severity}
              </span>
              <span className="px-3 py-1 rounded text-sm bg-gh-canvas border border-gh-border">
                {signature.status}
              </span>
            </div>
            <h2 className="text-xl font-semibold text-gh-fg">
              {signature.signature?.error_type}: {signature.signature?.normalized_message}
            </h2>
            <p className="text-sm text-gh-fg-muted mt-2 font-mono">
              {signature.signature?.error_pattern}
            </p>
          </div>
          <div className="flex gap-2">
            {(signature.investigation_status === 'not_started' || signature.investigation_status === 'failed') && (
              <button
                onClick={triggerInvestigation}
                className="px-4 py-2 bg-gh-accent-emphasis text-white rounded hover:bg-gh-accent-primary transition-colors flex items-center gap-2"
              >
                <Play className="w-4 h-4" />
                {signature.investigation_status === 'failed' ? 'Retry Investigation' : 'Start Investigation'}
              </button>
            )}
            {(signature.investigation_status === 'diagnosed' || signature.investigation_status === 'completed') && (
              <button
                onClick={triggerFix}
                className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 transition-colors flex items-center gap-2"
              >
                <Wrench className="w-4 h-4" />
                Launch Fix
              </button>
            )}
            <select
              value={signature.status}
              onChange={(e) => updateStatus(e.target.value)}
              className="px-3 py-2 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="new">New</option>
              <option value="recurring">Recurring</option>
              <option value="trending">Trending</option>
              <option value="resolved">Resolved</option>
              <option value="ignored">Ignored</option>
            </select>
          </div>
        </div>

        {/* Stats */}
        <div className="grid grid-cols-4 gap-4 mt-4">
          <div>
            <p className="text-xs text-gh-fg-muted">Total Occurrences</p>
            <p className="text-2xl font-bold mt-1">{signature.occurrence_count}</p>
          </div>
          <div>
            <p className="text-xs text-gh-fg-muted">Last Hour</p>
            <p className="text-2xl font-bold mt-1">{signature.occurrences_last_hour || 0}</p>
          </div>
          <div>
            <p className="text-xs text-gh-fg-muted">Last Day</p>
            <p className="text-2xl font-bold mt-1">{signature.occurrences_last_day || 0}</p>
          </div>
          <div>
            <p className="text-xs text-gh-fg-muted">Impact Score</p>
            <p className="text-2xl font-bold mt-1">{signature.impact_score?.toFixed(1) || 'N/A'}</p>
          </div>
        </div>

        {/* Timestamps */}
        <div className="grid grid-cols-3 gap-4 mt-4 text-sm">
          <div>
            <p className="text-xs text-gh-fg-muted">First Seen</p>
            <p className="mt-1">{formatTimestamp(signature.first_seen)}</p>
          </div>
          <div>
            <p className="text-xs text-gh-fg-muted">Last Seen</p>
            <p className="mt-1">{formatTimestamp(signature.last_seen)}</p>
          </div>
          <div>
            <p className="text-xs text-gh-fg-muted">Investigation Status</p>
            <p className="mt-1">{signature.investigation_status}</p>
          </div>
        </div>
      </div>

      {/* Stack Signature */}
      {signature.signature?.stack_signature && (
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
          <h3 className="text-sm font-semibold text-gh-fg mb-3 flex items-center gap-2">
            <Code className="w-4 h-4" />
            Stack Signature
          </h3>
          <div className="bg-gh-canvas border border-gh-border rounded p-3 font-mono text-xs">
            {signature.signature.stack_signature.map((frame, idx) => (
              <div key={idx} className="text-gh-fg-muted">
                {frame}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Investigation Reports */}
      {signature.investigation_status === 'completed' && (
        <div className="space-y-4">
          <button
            onClick={() => setShowDiagnosis(!showDiagnosis)}
            className="w-full bg-gh-canvas-subtle border border-gh-border rounded-lg p-4 hover:bg-gh-border-muted transition-colors text-left flex items-center justify-between"
          >
            <div className="flex items-center gap-2">
              <FileText className="w-4 h-4" />
              <span className="font-semibold">Root Cause Diagnosis</span>
            </div>
            <span className="text-xs text-gh-fg-muted">
              {showDiagnosis ? 'Hide' : 'Show'}
            </span>
          </button>
          {showDiagnosis && (
            <InvestigationReport
              fingerprintId={fingerprintId}
              reportType="diagnosis"
            />
          )}

          <button
            onClick={() => setShowFixPlan(!showFixPlan)}
            className="w-full bg-gh-canvas-subtle border border-gh-border rounded-lg p-4 hover:bg-gh-border-muted transition-colors text-left flex items-center justify-between"
          >
            <div className="flex items-center gap-2">
              <FileText className="w-4 h-4" />
              <span className="font-semibold">Fix Plan</span>
            </div>
            <span className="text-xs text-gh-fg-muted">
              {showFixPlan ? 'Hide' : 'Show'}
            </span>
          </button>
          {showFixPlan && (
            <InvestigationReport
              fingerprintId={fingerprintId}
              reportType="fix-plan"
            />
          )}
        </div>
      )}

      {/* Sample Occurrences */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gh-fg mb-3 flex items-center gap-2">
          <AlertCircle className="w-4 h-4" />
          Recent Occurrences
        </h3>
        <div className="space-y-3">
          {occurrences.length === 0 ? (
            <p className="text-sm text-gh-fg-muted">No occurrences found</p>
          ) : (
            occurrences.map((occurrence, idx) => (
              <div key={idx} className="bg-gh-canvas border border-gh-border rounded p-3">
                <div className="flex items-start justify-between mb-2">
                  <div className="flex items-center gap-2 text-xs text-gh-fg-muted">
                    <Clock className="w-3 h-3" />
                    <span>{formatTimestamp(occurrence.timestamp)}</span>
                    <span className="px-2 py-0.5 bg-gh-canvas-subtle rounded font-mono">
                      {occurrence.container_name}
                    </span>
                  </div>
                </div>
                {occurrence.context && (
                  <div className="text-xs text-gh-fg-muted mb-2">
                    {occurrence.context.agent && <span>Agent: {occurrence.context.agent} </span>}
                    {occurrence.context.project && <span>Project: {occurrence.context.project} </span>}
                    {occurrence.context.issue_number && <span>Issue: #{occurrence.context.issue_number}</span>}
                  </div>
                )}
                <pre className="text-xs text-gh-fg font-mono bg-gh-canvas-subtle p-2 rounded overflow-x-auto">
                  {occurrence.raw_message}
                </pre>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Occurrence Timeline Graph */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gh-fg mb-3">Occurrence Timeline</h3>
        <OccurrenceTimeline occurrences={occurrences} />
      </div>
    </div>
  )
}

function OccurrenceTimeline({ occurrences }) {
  if (!occurrences || occurrences.length === 0) {
    return <p className="text-sm text-gh-fg-muted">No data available</p>
  }

  // Group occurrences by hour
  const hourlyData = {}
  occurrences.forEach(occ => {
    const date = new Date(occ.timestamp)
    const hourKey = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:00`
    hourlyData[hourKey] = (hourlyData[hourKey] || 0) + 1
  })

  const hours = Object.keys(hourlyData).sort()
  const counts = hours.map(h => hourlyData[h])
  const maxCount = Math.max(...counts)

  return (
    <div className="space-y-2">
      {hours.map((hour, idx) => (
        <div key={hour} className="flex items-center gap-2">
          <span className="text-xs text-gh-fg-muted w-32 flex-shrink-0">{hour}</span>
          <div className="flex-1 bg-gh-canvas rounded h-6 relative">
            <div
              className="bg-gh-accent-emphasis rounded h-full"
              style={{ width: `${(counts[idx] / maxCount) * 100}%` }}
            />
            <span className="absolute right-2 top-0 bottom-0 flex items-center text-xs font-medium">
              {counts[idx]}
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}
