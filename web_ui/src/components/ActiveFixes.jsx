import { useEffect, useState } from 'react'
import { Wrench, Clock, XCircle, FileText, Loader2, Eye } from 'lucide-react'
import { useSocket } from '../contexts'
import { Link } from '@tanstack/react-router'

export default function ActiveFixes() {
  const [activeFixes, setActiveFixes] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedLog, setSelectedLog] = useState(null)
  const [logContent, setLogContent] = useState(null)
  const [loadingLog, setLoadingLog] = useState(false)
  const { medicEvents } = useSocket()

  useEffect(() => {
    fetchActiveFixes()
    const interval = setInterval(fetchActiveFixes, 5000) // Refresh every 5 seconds
    return () => clearInterval(interval)
  }, [])

  // Refresh when medic events occur
  useEffect(() => {
    if (medicEvents.length > 0) {
      const latestEvent = medicEvents[medicEvents.length - 1]
      if (latestEvent.event_type?.includes('medic_fix')) {
        fetchActiveFixes()
      }
    }
  }, [medicEvents])

  const fetchActiveFixes = async () => {
    try {
      const response = await fetch('/fix-orchestrator/active')
      if (!response.ok) throw new Error('Failed to fetch active fixes')
      const data = await response.json()
      setActiveFixes(data)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const killFix = async (fingerprintId) => {
    if (!confirm('Are you sure you want to kill this fix execution?')) {
      return
    }

    try {
      const response = await fetch(`/fix-orchestrator/kill/${fingerprintId}`, {
        method: 'POST'
      })
      if (!response.ok) throw new Error('Failed to kill fix')

      // Refresh the list
      await fetchActiveFixes()
    } catch (err) {
      alert(`Error killing fix: ${err.message}`)
    }
  }

  const viewLog = async (fingerprintId) => {
    setSelectedLog(fingerprintId)
    setLoadingLog(true)
    setLogContent(null)

    try {
      const response = await fetch(`/api/medic/claude/fixes/${fingerprintId}/log`)
      if (!response.ok) throw new Error('Failed to fetch log')
      const data = await response.json()
      setLogContent(data)
    } catch (err) {
      setLogContent({ error: err.message })
    } finally {
      setLoadingLog(false)
    }
  }

  const closeLogModal = () => {
    setSelectedLog(null)
    setLogContent(null)
  }

  const formatTimestamp = (isoString) => {
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
      case 'queued':
        return 'text-yellow-500 bg-yellow-500/10 border-yellow-500/20'
      case 'starting':
        return 'text-orange-500 bg-orange-500/10 border-orange-500/20'
      case 'in_progress':
        return 'text-blue-500 bg-blue-500/10 border-blue-500/20'
      default:
        return 'text-gray-500 bg-gray-500/10 border-gray-500/20'
    }
  }

  const getStatusLabel = (status) => {
    switch (status) {
      case 'queued':
        return 'Queued'
      case 'starting':
        return 'Starting'
      case 'in_progress':
        return 'Running'
      default:
        return status
    }
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
        <p className="text-sm text-red-500">Error loading active fixes: {error}</p>
      </div>
    )
  }

  if (activeFixes.length === 0) {
    return (
      <div className="p-8 text-center bg-gh-canvas-subtle border border-gh-border rounded-lg">
        <Wrench className="w-12 h-12 mx-auto mb-3 text-gh-fg-muted" />
        <p className="text-gh-fg-muted">No active fix executions</p>
      </div>
    )
  }

  return (
    <>
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-gh-canvas-subtle border-b border-gh-border">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gh-fg">Fingerprint</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gh-fg">Status</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gh-fg">Project</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gh-fg">Started</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gh-fg">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gh-border">
              {activeFixes.map((fix) => (
                <tr key={fix.fingerprint_id} className="hover:bg-gh-canvas-inset">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      {fix.status === 'queued' ? (
                        <Clock className="w-4 h-4 text-yellow-500" />
                      ) : (
                        <Loader2 className="w-4 h-4 animate-spin text-blue-500" />
                      )}
                      <code className="text-xs text-gh-fg font-mono">
                        {fix.fingerprint_id.substring(0, 16)}...
                      </code>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded text-xs font-medium border ${getStatusColor(fix.status)}`}>
                      {getStatusLabel(fix.status)}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-sm text-gh-fg">{fix.project || 'unknown'}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 text-sm text-gh-fg-muted">
                      <Clock className="w-3 h-3" />
                      {formatTimestamp(fix.started_at)}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-2">
                      {fix.agent_execution_id && (
                        <Link
                          to="/agent-execution/$executionId"
                          params={{ executionId: fix.agent_execution_id }}
                          className="px-2 py-1 text-xs text-green-500 hover:text-green-400 hover:bg-green-500/10 rounded transition-colors flex items-center gap-1"
                          title="View live execution"
                        >
                          <Eye className="w-3 h-3" />
                          Live
                        </Link>
                      )}
                      <button
                        onClick={() => viewLog(fix.fingerprint_id)}
                        className="px-2 py-1 text-xs text-blue-500 hover:text-blue-400 hover:bg-blue-500/10 rounded transition-colors flex items-center gap-1"
                        title="View logs"
                      >
                        <FileText className="w-3 h-3" />
                        Logs
                      </button>
                      <button
                        onClick={() => killFix(fix.fingerprint_id)}
                        className="px-2 py-1 text-xs text-red-500 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors flex items-center gap-1"
                        title="Kill fix execution"
                      >
                        <XCircle className="w-3 h-3" />
                        Kill
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Log Modal */}
      {selectedLog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-gh-canvas border border-gh-border rounded-lg max-w-4xl w-full max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between p-4 border-b border-gh-border">
              <h3 className="text-lg font-semibold text-gh-fg">Fix Execution Log</h3>
              <button
                onClick={closeLogModal}
                className="text-gh-fg-muted hover:text-gh-fg"
              >
                <XCircle className="w-5 h-5" />
              </button>
            </div>
            <div className="flex-1 overflow-auto p-4">
              {loadingLog ? (
                <div className="flex items-center justify-center p-8">
                  <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
                </div>
              ) : logContent?.error ? (
                <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
                  <p className="text-sm text-red-500">Error loading log: {logContent.error}</p>
                </div>
              ) : logContent?.logs && logContent.logs.length > 0 ? (
                <div className="space-y-2">
                  {logContent.logs.map((entry, idx) => (
                    <div key={idx} className="p-2 bg-gh-canvas-subtle rounded border border-gh-border">
                      <pre className="text-xs text-gh-fg font-mono whitespace-pre-wrap break-all">
                        {JSON.stringify(entry, null, 2)}
                      </pre>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gh-fg-muted text-center">No logs available yet</p>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
