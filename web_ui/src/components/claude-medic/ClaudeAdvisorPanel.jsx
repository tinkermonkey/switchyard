import { useState, useEffect } from 'react'
import { Play, FileText, RefreshCw, Clock, CheckCircle, Loader2 } from 'lucide-react'
import Modal from '../Modal'
import ReactMarkdown from 'react-markdown'
import { useProjects } from '../../hooks/useProjects'

export default function ClaudeAdvisorPanel() {
  const { projects } = useProjects()
  const [activeRuns, setActiveRuns] = useState([])
  const [reports, setReports] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedReport, setSelectedReport] = useState(null)
  const [reportContent, setReportContent] = useState('')
  const [loadingReport, setLoadingReport] = useState(false)
  const [selectedProject, setSelectedProject] = useState('')
  const [startingRun, setStartingRun] = useState(false)

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 10000) // Refresh every 10s
    return () => clearInterval(interval)
  }, [])

  const fetchData = async () => {
    try {
      const [runsRes, reportsRes] = await Promise.all([
        fetch('/api/medic/claude/advisor/active'),
        fetch('/api/medic/claude/advisor/reports')
      ])
      
      if (runsRes.ok) {
        const data = await runsRes.json()
        setActiveRuns(data.runs || [])
      }
      
      if (reportsRes.ok) {
        const data = await reportsRes.json()
        setReports(data.reports || [])
      }
    } catch (error) {
      console.error('Error fetching advisor data:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleRunAdvisor = async () => {
    if (!selectedProject) return
    setStartingRun(true)
    try {
      const res = await fetch(`/api/medic/claude/advisor/projects/${selectedProject}/run`, {
        method: 'POST'
      })
      if (res.ok) {
        fetchData() // Refresh active runs
        setSelectedProject('') // Reset selection
      } else {
        console.error('Failed to start advisor run')
      }
    } catch (error) {
      console.error('Error starting advisor run:', error)
    } finally {
      setStartingRun(false)
    }
  }

  const viewReport = async (report) => {
    setSelectedReport(report)
    setLoadingReport(true)
    try {
      const res = await fetch(`/api/medic/claude/advisor/projects/${report.project}/reports/${report.filename}`)
      if (res.ok) {
        const data = await res.json()
        setReportContent(data.content)
      } else {
        setReportContent('Failed to load report content.')
      }
    } catch (error) {
      setReportContent('Error loading report content.')
    } finally {
      setLoadingReport(false)
    }
  }

  return (
    <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4 h-full">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-gh-fg flex items-center gap-2">
          <FileText className="w-4 h-4" />
          Claude Advisor
        </h3>
        <button 
          onClick={fetchData}
          className="p-1 hover:bg-gh-canvas-default rounded text-gh-fg-muted hover:text-gh-fg"
          title="Refresh"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="space-y-4">
        {/* Start Analysis Section */}
        <div className="bg-gh-canvas border border-gh-border rounded p-3">
          <h4 className="text-xs font-semibold text-gh-fg-muted uppercase tracking-wider mb-2">Start Analysis</h4>
          <div className="flex gap-2">
            <select
              value={selectedProject}
              onChange={(e) => setSelectedProject(e.target.value)}
              className="flex-1 px-2 py-1 bg-gh-canvas-subtle border border-gh-border rounded text-sm text-gh-fg focus:border-gh-accent-emphasis outline-none"
            >
              <option value="">Select Project...</option>
              {projects.map(p => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
            <button
              onClick={handleRunAdvisor}
              disabled={!selectedProject || startingRun}
              className="px-3 py-1 bg-gh-accent-emphasis text-white rounded text-sm hover:bg-gh-accent-primary disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1 transition-colors"
            >
              {startingRun ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              Run
            </button>
          </div>
        </div>

        {/* Active Runs Section */}
        {activeRuns.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-xs font-semibold text-gh-fg-muted uppercase tracking-wider">Active Runs</h4>
            {activeRuns.map((run, idx) => (
              <div key={idx} className="bg-gh-canvas border border-blue-500/30 rounded p-3 flex items-center justify-between animate-pulse">
                <div className="flex items-center gap-2">
                  <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
                  <div>
                    <p className="text-sm font-medium text-gh-fg">{run.project}</p>
                    <p className="text-xs text-gh-fg-muted">Analyzing failures...</p>
                  </div>
                </div>
                <span className="text-xs text-blue-500 font-mono">RUNNING</span>
              </div>
            ))}
          </div>
        )}

        {/* Reports List */}
        <div className="space-y-2">
          <h4 className="text-xs font-semibold text-gh-fg-muted uppercase tracking-wider">Recent Reports</h4>
          {reports.length === 0 ? (
            <p className="text-sm text-gh-fg-muted italic">No reports found.</p>
          ) : (
            <div className="space-y-2 max-h-[300px] overflow-y-auto pr-1">
              {reports.map((report, idx) => (
                <div 
                  key={idx} 
                  className="bg-gh-canvas border border-gh-border rounded p-3 hover:border-gh-accent-emphasis cursor-pointer transition-colors"
                  onClick={() => viewReport(report)}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm font-medium text-gh-fg">{report.project}</span>
                    <span className="text-xs text-gh-fg-muted flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {new Date(report.created_at).toLocaleDateString()}
                    </span>
                  </div>
                  <p className="text-xs text-gh-fg-muted truncate">{report.filename}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Report Modal */}
      {selectedReport && (
        <Modal
          onClose={() => setSelectedReport(null)}
          title={`Advisor Report: ${selectedReport.project}`}
        >
          <div className="p-6">
            {loadingReport ? (
              <div className="flex justify-center py-12">
                <Loader2 className="w-8 h-8 text-gh-accent-emphasis animate-spin" />
              </div>
            ) : (
              <div className="prose prose-invert max-w-none">
                <ReactMarkdown>{reportContent}</ReactMarkdown>
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}
