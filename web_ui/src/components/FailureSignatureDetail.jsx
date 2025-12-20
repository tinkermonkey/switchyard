import { useEffect, useState } from 'react'
import { AlertCircle, Clock, Code, Play, FileText, Wrench, Trash2, ChevronDown, Loader2 } from 'lucide-react'
import { useNavigate } from '@tanstack/react-router'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import InvestigationReport from './InvestigationReport'
import ConfirmationModal from './ConfirmationModal'
import Toast from './Toast'
import { useSocket } from '../contexts'

export default function FailureSignatureDetail({ fingerprintId }) {
  const [signature, setSignature] = useState(null)
  const [occurrences, setOccurrences] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showDiagnosis, setShowDiagnosis] = useState(false)
  const [showFixPlan, setShowFixPlan] = useState(false)
  const [showFixResults, setShowFixResults] = useState(false)
  const [showDeleteModal, setShowDeleteModal] = useState(false)
  const [modalConfig, setModalConfig] = useState({ show: false, title: '', message: '', isDangerous: false })
  const [fixStatus, setFixStatus] = useState(null)
  const [fixLog, setFixLog] = useState(null)
  const [toast, setToast] = useState({ show: false, message: '', type: 'info' })
  const { medicEvents } = useSocket()
  const navigate = useNavigate()

  useEffect(() => {
    fetchSignatureDetails()
    fetchOccurrences()
    fetchFixStatus()
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
        fetchFixStatus()
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

  const fetchFixStatus = async () => {
    try {
      const response = await fetch(`/api/medic/claude/fixes/${fingerprintId}/status`)
      if (response.ok) {
        const data = await response.json()
        setFixStatus(data.status || null)
        // If fix is completed, also fetch the log
        if (data.status === 'completed' && !fixLog) {
          fetchFixLog()
        }
      } else {
        setFixStatus(null)
      }
    } catch (err) {
      console.error('Error fetching fix status:', err)
      setFixStatus(null)
    }
  }

  const fetchFixLog = async () => {
    try {
      const response = await fetch(`/api/medic/claude/fixes/${fingerprintId}/log`)
      if (response.ok) {
        const data = await response.json()
        setFixLog(data)
      }
    } catch (err) {
      console.error('Error fetching fix log:', err)
    }
  }

  const triggerInvestigation = async () => {
    try {
      const response = await fetch(`/api/medic/investigations/${fingerprintId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ priority: 'high' })
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.error || 'Failed to trigger investigation')
      }
      fetchSignatureDetails() // Refresh to update investigation status
    } catch (err) {
      setModalConfig({
        show: true,
        title: 'Investigation Failed',
        message: err.message,
        isDangerous: true,
        onConfirm: () => setModalConfig(prev => ({ ...prev, show: false })),
        confirmText: 'Close',
        cancelText: null
      })
    }
  }

  const triggerFix = async () => {
    try {
      const response = await fetch(`/api/medic/claude/fixes/${fingerprintId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.error || 'Failed to trigger fix')
      }
      // Show success toast
      setToast({
        show: true,
        message: 'Fix has been queued successfully and will be executed shortly.',
        type: 'success'
      })
      // Refresh data
      fetchSignatureDetails()
      fetchFixStatus()
    } catch (err) {
      setModalConfig({
        show: true,
        title: 'Fix Failed',
        message: err.message,
        isDangerous: true,
        onConfirm: () => setModalConfig(prev => ({ ...prev, show: false })),
        confirmText: 'Close',
        cancelText: null
      })
    }
  }

  const updateStatus = async (newStatus) => {
    try {
      const response = await fetch(`/api/medic/failure-signatures/${fingerprintId}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus })
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.error || 'Failed to update status')
      }
      fetchSignatureDetails()
    } catch (err) {
      setModalConfig({
        show: true,
        title: 'Update Failed',
        message: err.message,
        isDangerous: true,
        onConfirm: () => setModalConfig(prev => ({ ...prev, show: false })),
        confirmText: 'Close',
        cancelText: null
      })
    }
  }

  const deleteSignature = async () => {
    try {
      const response = await fetch(`/api/medic/failure-signatures/${fingerprintId}`, {
        method: 'DELETE'
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.error || 'Failed to delete signature')
      }
      navigate({ to: '/medic' })
    } catch (err) {
      setModalConfig({
        show: true,
        title: 'Delete Failed',
        message: err.message,
        isDangerous: true,
        onConfirm: () => setModalConfig(prev => ({ ...prev, show: false })),
        confirmText: 'Close',
        cancelText: null
      })
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
              {signature.investigation_status === 'queued' && (
                <span className="px-3 py-1 rounded text-sm font-medium bg-yellow-500/10 text-yellow-500 border border-yellow-500/20 flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  Investigation Queued
                </span>
              )}
              {(signature.investigation_status === 'in_progress' || signature.investigation_status === 'starting') && (
                <span className="px-3 py-1 rounded text-sm font-medium bg-blue-500/10 text-blue-500 border border-blue-500/20 flex items-center gap-1">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Investigation Running
                </span>
              )}
              {signature.investigation_status === 'completed' && (
                <span className="px-3 py-1 rounded text-sm font-medium bg-green-500/10 text-green-500 border border-green-500/20 flex items-center gap-1">
                  <FileText className="w-3 h-3" />
                  Investigation Complete
                </span>
              )}
              {signature.investigation_status === 'failed' && (
                <span className="px-3 py-1 rounded text-sm font-medium bg-red-500/10 text-red-500 border border-red-500/20 flex items-center gap-1">
                  <AlertCircle className="w-3 h-3" />
                  Investigation Failed
                </span>
              )}
            </div>
            <h2 className="text-xl font-semibold text-gh-fg">
              {signature.signature?.error_type}: {signature.signature?.normalized_message}
            </h2>
            <p className="text-xs text-gh-fg-muted mt-1 font-mono select-all">
              ID: {fingerprintId}
            </p>
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
              <>
                {fixStatus === 'queued' || fixStatus === 'starting' || fixStatus === 'in_progress' ? (
                  <button
                    disabled
                    className="px-4 py-2 bg-gray-500 text-white rounded cursor-not-allowed opacity-50 flex items-center gap-2"
                    title={
                      fixStatus === 'queued' ? 'Fix is queued for execution' :
                      fixStatus === 'starting' ? 'Fix is starting' :
                      'Fix is in progress'
                    }
                  >
                    <Wrench className="w-4 h-4" />
                    {fixStatus === 'queued' && 'Fix Queued'}
                    {fixStatus === 'starting' && 'Fix Starting...'}
                    {fixStatus === 'in_progress' && 'Fix In Progress...'}
                  </button>
                ) : fixStatus === 'completed' ? (
                  <span className="px-4 py-2 bg-green-500/10 text-green-500 rounded flex items-center gap-2 font-medium">
                    <Wrench className="w-4 h-4" />
                    Fixed ✓
                  </span>
                ) : fixStatus === 'failed' ? (
                  <button
                    onClick={triggerFix}
                    className="px-4 py-2 bg-orange-600 text-white rounded hover:bg-orange-700 transition-colors flex items-center gap-2"
                    title="Previous fix failed - click to retry"
                  >
                    <Wrench className="w-4 h-4" />
                    Retry Fix
                  </button>
                ) : (
                  <button
                    onClick={triggerFix}
                    className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 transition-colors flex items-center gap-2"
                    title="Launch fix for this failure"
                  >
                    <Wrench className="w-4 h-4" />
                    Launch Fix
                  </button>
                )}
              </>
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
            <button
              onClick={() => setShowDeleteModal(true)}
              className="px-3 py-2 bg-red-600/10 text-red-600 border border-red-600/20 rounded hover:bg-red-600/20 transition-colors"
              title="Delete Signature"
            >
              <Trash2 className="w-4 h-4" />
            </button>
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

      {/* Stack / Context Signature */}
      {(signature.signature?.stack_signature?.length > 0 || signature.signature?.context_signature) && (
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
          <h3 className="text-sm font-semibold text-gh-fg mb-3 flex items-center gap-2">
            <Code className="w-4 h-4" />
            {signature.signature?.context_signature ? 'Context Signature' : 'Stack Signature'}
          </h3>
          <div className="bg-gh-canvas border border-gh-border rounded p-3 font-mono text-xs overflow-x-auto">
            {signature.signature?.stack_signature?.length > 0 ? (
              signature.signature.stack_signature.map((frame, idx) => (
                <div key={idx} className="text-gh-fg-muted whitespace-pre">
                  {frame}
                </div>
              ))
            ) : (
              <div className="text-gh-fg-muted whitespace-pre">
                {signature.signature?.context_signature}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Investigation Reports */}
      {signature.investigation_status === 'completed' && (
        <div className="space-y-4">
          {/* Root Cause Diagnosis */}
          <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg overflow-hidden">
            <button
              onClick={() => setShowDiagnosis(!showDiagnosis)}
              className="w-full p-4 hover:bg-gh-border-muted transition-colors text-left flex items-center justify-between"
            >
              <div className="flex items-center gap-2">
                <FileText className="w-4 h-4" />
                <span className="font-semibold">Root Cause Diagnosis</span>
              </div>
              <ChevronDown
                className={`w-5 h-5 text-gh-fg-muted transition-transform duration-200 ${
                  showDiagnosis ? 'transform rotate-180' : ''
                }`}
              />
            </button>
            {showDiagnosis && (
              <div className="border-t border-gh-border p-4">
                <InvestigationReport
                  fingerprintId={fingerprintId}
                  reportType="diagnosis"
                />
              </div>
            )}
          </div>

          {/* Fix Plan */}
          <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg overflow-hidden">
            <button
              onClick={() => setShowFixPlan(!showFixPlan)}
              className="w-full p-4 hover:bg-gh-border-muted transition-colors text-left flex items-center justify-between"
            >
              <div className="flex items-center gap-2">
                <FileText className="w-4 h-4" />
                <span className="font-semibold">Fix Plan</span>
              </div>
              <ChevronDown
                className={`w-5 h-5 text-gh-fg-muted transition-transform duration-200 ${
                  showFixPlan ? 'transform rotate-180' : ''
                }`}
              />
            </button>
            {showFixPlan && (
              <div className="border-t border-gh-border p-4">
                <InvestigationReport
                  fingerprintId={fingerprintId}
                  reportType="fix-plan"
                />
              </div>
            )}
          </div>

          {/* Fix Results (only show if fix is completed) */}
          {fixStatus === 'completed' && (
            <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg overflow-hidden">
              <button
                onClick={() => {
                  setShowFixResults(!showFixResults)
                  if (!showFixResults && !fixLog) {
                    fetchFixLog()
                  }
                }}
                className="w-full p-4 hover:bg-gh-border-muted transition-colors text-left flex items-center justify-between"
              >
                <div className="flex items-center gap-2">
                  <Wrench className="w-4 h-4 text-green-500" />
                  <span className="font-semibold">Fix Results</span>
                  <span className="ml-2 px-2 py-0.5 bg-green-500/10 text-green-500 rounded text-xs">
                    Completed ✓
                  </span>
                </div>
                <ChevronDown
                  className={`w-5 h-5 text-gh-fg-muted transition-transform duration-200 ${
                    showFixResults ? 'transform rotate-180' : ''
                  }`}
                />
              </button>
              {showFixResults && (
                <div className="border-t border-gh-border p-4">
                  {fixLog && fixLog.logs && fixLog.logs.length > 0 ? (
                    <div className="space-y-4">
                      {/* Extract summary from last message */}
                      {(() => {
                        const lastMessage = fixLog.logs.filter(log => log.type === 'assistant').pop()
                        const summaryMessage = lastMessage?.message?.content?.find(c => c.type === 'text')?.text
                        if (summaryMessage) {
                          return (
                            <div className="bg-gh-canvas border border-gh-border rounded p-4">
                              <h4 className="text-sm font-semibold text-gh-fg mb-3">Summary</h4>
                              <div className="prose prose-invert prose-sm max-w-none">
                                <ReactMarkdown
                                  components={{
                                    code({ node, inline, className, children, ...props }) {
                                      const match = /language-(\w+)/.exec(className || '')
                                      return !inline && match ? (
                                        <SyntaxHighlighter
                                          style={vscDarkPlus}
                                          language={match[1]}
                                          PreTag="div"
                                          {...props}
                                        >
                                          {String(children).replace(/\n$/, '')}
                                        </SyntaxHighlighter>
                                      ) : (
                                        <code className="bg-gh-canvas px-1.5 py-0.5 rounded text-gh-accent-fg font-mono text-xs" {...props}>
                                          {children}
                                        </code>
                                      )
                                    },
                                    h1: ({ children }) => (
                                      <h1 className="text-xl font-bold text-gh-fg mt-4 mb-3 pb-2 border-b border-gh-border">
                                        {children}
                                      </h1>
                                    ),
                                    h2: ({ children }) => (
                                      <h2 className="text-lg font-bold text-gh-fg mt-3 mb-2">
                                        {children}
                                      </h2>
                                    ),
                                    h3: ({ children }) => (
                                      <h3 className="text-base font-semibold text-gh-fg mt-3 mb-2">
                                        {children}
                                      </h3>
                                    ),
                                    p: ({ children }) => (
                                      <p className="text-sm text-gh-fg mb-3 leading-relaxed">
                                        {children}
                                      </p>
                                    ),
                                    ul: ({ children }) => (
                                      <ul className="list-disc list-inside text-sm text-gh-fg mb-3 space-y-1">
                                        {children}
                                      </ul>
                                    ),
                                    ol: ({ children }) => (
                                      <ol className="list-decimal list-inside text-sm text-gh-fg mb-3 space-y-1">
                                        {children}
                                      </ol>
                                    ),
                                    li: ({ children }) => (
                                      <li className="text-gh-fg">
                                        {children}
                                      </li>
                                    ),
                                    a: ({ children, href }) => (
                                      <a
                                        href={href}
                                        className="text-gh-accent-fg hover:underline"
                                        target="_blank"
                                        rel="noopener noreferrer"
                                      >
                                        {children}
                                      </a>
                                    ),
                                    blockquote: ({ children }) => (
                                      <blockquote className="border-l-4 border-gh-accent-emphasis pl-4 italic text-gh-fg-muted mb-3">
                                        {children}
                                      </blockquote>
                                    ),
                                  }}
                                >
                                  {summaryMessage}
                                </ReactMarkdown>
                              </div>
                            </div>
                          )
                        }
                        return null
                      })()}

                      {/* Full execution log (collapsed by default) */}
                      <details className="bg-gh-canvas border border-gh-border rounded">
                        <summary className="p-3 cursor-pointer hover:bg-gh-canvas-subtle text-sm font-medium">
                          View Full Execution Log ({fixLog.logs.length} entries)
                        </summary>
                        <div className="border-t border-gh-border p-3 max-h-96 overflow-y-auto">
                          <pre className="text-xs text-gh-fg font-mono whitespace-pre-wrap">
                            {JSON.stringify(fixLog.logs, null, 2)}
                          </pre>
                        </div>
                      </details>
                    </div>
                  ) : (
                    <p className="text-sm text-gh-fg-muted">Loading fix results...</p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Fix Failed Status */}
          {fixStatus === 'failed' && (
            <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-4">
              <div className="flex items-center gap-2 text-red-500">
                <AlertCircle className="w-4 h-4" />
                <span className="font-semibold">Fix execution failed</span>
              </div>
              <p className="text-sm text-gh-fg-muted mt-2">
                The automated fix failed to complete. You can retry the fix or investigate manually.
              </p>
            </div>
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

      <ConfirmationModal
        show={showDeleteModal}
        onClose={() => setShowDeleteModal(false)}
        onConfirm={deleteSignature}
        title="Delete Failure Signature"
        message="Are you sure you want to delete this failure signature? This will also delete all associated investigation reports and assets. This action cannot be undone."
        confirmText="Delete"
        isDangerous={true}
      />

      <ConfirmationModal
        show={modalConfig.show}
        onClose={() => setModalConfig(prev => ({ ...prev, show: false }))}
        onConfirm={modalConfig.onConfirm}
        title={modalConfig.title}
        message={modalConfig.message}
        confirmText={modalConfig.confirmText}
        cancelText={modalConfig.cancelText}
        isDangerous={modalConfig.isDangerous}
      />

      <Toast
        show={toast.show}
        onClose={() => setToast(prev => ({ ...prev, show: false }))}
        message={toast.message}
        type={toast.type}
      />
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
