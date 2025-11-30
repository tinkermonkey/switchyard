import { useEffect, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { AlertCircle, TrendingUp, Clock, Play, Code2, FolderGit2, Brain } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useSocket } from '../../contexts'
import ProjectFilter from './ProjectFilter'
import Modal from '../Modal'

export default function ClaudeFailureSignatureList() {
  const [signatures, setSignatures] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filters, setFilters] = useState({
    project: '',
    status: '',
    investigation_status: '',
    tool_name: '',
  })
  const [sortBy, setSortBy] = useState('last_seen')
  const [sortOrder, setSortOrder] = useState('desc')
  
  // Advisor State
  const [showAdvisor, setShowAdvisor] = useState(false)
  const [advisorReports, setAdvisorReports] = useState([])
  const [selectedReport, setSelectedReport] = useState(null)
  const [selectedReportContent, setSelectedReportContent] = useState(null)
  const [advisorLoading, setAdvisorLoading] = useState(false)

  const { medicEvents } = useSocket()

  useEffect(() => {
    fetchSignatures()
  }, [filters, sortBy, sortOrder])

  // Refresh list when medic events occur
  useEffect(() => {
    if (medicEvents.length > 0) {
      const lastEvent = medicEvents[0]
      // Only refresh for Claude Medic events
      if (lastEvent.event_type?.includes('claude') || lastEvent.type === 'claude_failure') {
        fetchSignatures()
      }
    }
  }, [medicEvents])

  // Fetch advisor reports when modal opens
  useEffect(() => {
    if (showAdvisor && filters.project) {
      fetchAdvisorReports()
    }
  }, [showAdvisor, filters.project])

  const fetchSignatures = async () => {
    try {
      setLoading(true)
      const params = new URLSearchParams()
      if (filters.project) params.append('project', filters.project)
      if (filters.status) params.append('status', filters.status)
      if (filters.investigation_status) params.append('investigation_status', filters.investigation_status)
      if (filters.tool_name) params.append('tool_name', filters.tool_name)
      params.append('limit', '50')

      const response = await fetch(`/api/medic/claude/failure-signatures?${params}`)
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

  const fetchAdvisorReports = async () => {
    try {
      const response = await fetch(`/api/medic/claude/advisor/projects/${filters.project}/reports`)
      if (!response.ok) throw new Error('Failed to fetch reports')
      const data = await response.json()
      setAdvisorReports(data.reports || [])
      
      // If reports exist and none selected, select the first one (most recent usually)
      if (data.reports && data.reports.length > 0 && !selectedReport) {
        viewReport(data.reports[0])
      }
    } catch (err) {
      console.error('Error fetching advisor reports:', err)
    }
  }

  const runAdvisor = async () => {
    if (!filters.project) return
    
    try {
      setAdvisorLoading(true)
      const response = await fetch(`/api/medic/claude/advisor/projects/${filters.project}/run`, {
        method: 'POST'
      })
      
      if (!response.ok) throw new Error('Failed to start advisor')
      
      // Poll for new report or just wait a bit and refresh list
      // Since it's async, we just tell the user it started
      alert('Advisor analysis started. This may take a few minutes.')
      
      // Refresh reports list after a delay to see if a new one appeared (unlikely to be instant)
      setTimeout(fetchAdvisorReports, 2000)
      
    } catch (err) {
      alert(`Error starting advisor: ${err.message}`)
    } finally {
      setAdvisorLoading(false)
    }
  }

  const viewReport = async (filename) => {
    try {
      setSelectedReport(filename)
      const response = await fetch(`/api/medic/claude/advisor/projects/${filters.project}/reports/${filename}`)
      if (!response.ok) throw new Error('Failed to fetch report content')
      const data = await response.json()
      setSelectedReportContent(data.content)
    } catch (err) {
      console.error('Error fetching report content:', err)
      setSelectedReportContent('Error loading report content.')
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
        case 'cluster_count':
          aVal = a.cluster_count
          bVal = b.cluster_count
          break
        case 'total_failures':
          aVal = a.total_failures
          bVal = b.total_failures
          break
        default:
          return 0
      }

      return order === 'asc' ? (aVal > bVal ? 1 : -1) : (aVal < bVal ? 1 : -1)
    })
  }

  const triggerInvestigation = async (fingerprintId) => {
    try {
      const response = await fetch(`/api/medic/claude/investigations/${fingerprintId}`, {
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
        <div className="flex flex-wrap gap-4 items-center">
          <ProjectFilter
            selectedProject={filters.project}
            onProjectChange={(project) => setFilters({...filters, project})}
          />

          {/* Advisor Button */}
          <button
            onClick={() => setShowAdvisor(true)}
            disabled={!filters.project}
            className={`px-3 py-1.5 rounded text-sm flex items-center gap-2 border transition-colors ${
              filters.project
                ? 'bg-gh-canvas hover:bg-gh-canvas-subtle border-gh-border text-gh-fg'
                : 'bg-gh-canvas-subtle border-gh-border text-gh-fg-muted cursor-not-allowed'
            }`}
            title={!filters.project ? "Select a project to use Advisor" : "Open Project Advisor"}
          >
            <Brain className="w-4 h-4" />
            Advisor
          </button>

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
            <label className="block text-xs text-gh-fg-muted mb-1">Tool</label>
            <select
              value={filters.tool_name}
              onChange={(e) => setFilters({...filters, tool_name: e.target.value})}
              className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="">All Tools</option>
              <option value="Bash">Bash</option>
              <option value="Read">Read</option>
              <option value="Edit">Edit</option>
              <option value="Write">Write</option>
              <option value="Grep">Grep</option>
              <option value="Glob">Glob</option>
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
              <option value="cluster_count">Cluster Count</option>
              <option value="total_failures">Total Failures</option>
            </select>
          </div>

          <div>
            <label className="block text-xs text-gh-fg-muted mb-1">Order by</label>
            <select
              value={sortOrder}
              onChange={(e) => setSortOrder(e.target.value)}
              className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm"
            >
              <option value="desc">Age (desc)</option>
              <option value="asc">Age (asc)</option>
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
            <ClaudeFailureSignatureCard
              key={sig.fingerprint_id}
              signature={sig}
              onTriggerInvestigation={triggerInvestigation}
            />
          ))}
        </div>
      )}

      {/* Advisor Modal */}
      {showAdvisor && (
        <Modal
          title={`Claude Advisor: ${filters.project}`}
          onClose={() => setShowAdvisor(false)}
        >
          <div className="space-y-6">
            <div className="flex justify-between items-center">
              <h4 className="text-lg font-semibold text-gh-fg">Analysis Reports</h4>
              <button
                onClick={runAdvisor}
                disabled={advisorLoading}
                className="px-4 py-2 bg-gh-accent-emphasis text-white rounded hover:bg-gh-accent-primary disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
              >
                {advisorLoading ? (
                  <>
                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                    Running Analysis...
                  </>
                ) : (
                  <>
                    <Brain className="w-4 h-4" />
                    Run New Analysis
                  </>
                )}
              </button>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-4 gap-6 h-[calc(100vh-300px)]">
              {/* Report List */}
              <div className="lg:col-span-1 border-r border-gh-border pr-4 overflow-y-auto">
                {advisorReports.length === 0 ? (
                  <p className="text-gh-fg-muted text-sm">No reports found.</p>
                ) : (
                  <div className="space-y-2">
                    {advisorReports.map((report) => (
                      <button
                        key={report}
                        onClick={() => viewReport(report)}
                        className={`w-full text-left px-3 py-2 rounded text-sm truncate transition-colors ${
                          selectedReport === report
                            ? 'bg-gh-accent-emphasis text-white'
                            : 'hover:bg-gh-canvas-subtle text-gh-fg'
                        }`}
                      >
                        {report.replace('advisor_report_', '').replace('.md', '').replace(/_/g, ' ')}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Report Content */}
              <div className="lg:col-span-3 overflow-y-auto bg-gh-canvas p-6 rounded border border-gh-border">
                {selectedReportContent ? (
                  <div className="prose prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {selectedReportContent}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="flex flex-col items-center justify-center h-full text-gh-fg-muted">
                    <Brain className="w-12 h-12 mb-4 opacity-20" />
                    <p>Select a report to view details</p>
                  </div>
                )}
              </div>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}

function ClaudeFailureSignatureCard({ signature, onTriggerInvestigation }) {
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
      to={`/claude-medic-detail/${signature.fingerprint_id}`}
      className="block bg-gh-canvas-subtle border border-gh-border rounded-lg p-4 hover:bg-gh-border-muted transition-colors"
    >
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2 flex-wrap">
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${getStatusColor(signature.status)}`}>
              {signature.status}
            </span>
            <span className="px-2 py-0.5 rounded text-xs bg-gh-canvas border border-gh-border flex items-center gap-1">
              <FolderGit2 className="w-3 h-3" />
              {signature.project}
            </span>
            <span className="px-2 py-0.5 rounded text-xs bg-gh-canvas border border-gh-border flex items-center gap-1">
              <Code2 className="w-3 h-3" />
              {signature.signature?.tool_name}
            </span>
          </div>

          <div className="mb-2">
            <p className="text-sm font-medium text-gh-fg truncate">
              {signature.signature?.error_type}: {signature.signature?.error_pattern}
            </p>
            {signature.signature?.context_signature && (
              <p className="text-xs text-gh-fg-muted mt-1 font-mono">
                Context: {signature.signature.context_signature}
              </p>
            )}
          </div>

          <div className="flex items-center gap-4 text-xs text-gh-fg-muted flex-wrap">
            <div className="flex items-center gap-1">
              <TrendingUp className="w-3 h-3" />
              <span>{signature.cluster_count || 0} clusters</span>
            </div>
            <div className="flex items-center gap-1">
              <AlertCircle className="w-3 h-3" />
              <span>{signature.total_failures || 0} total failures</span>
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
