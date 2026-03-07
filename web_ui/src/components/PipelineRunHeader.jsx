import { useState, useEffect, useRef } from 'react'
import { Lock, Unlock, Clock, XCircle, ArrowRight, MessageSquare, Copy, Maximize2, Minimize2, CheckCircle2, AlertCircle, X } from 'lucide-react'
import { useNavigate } from '@tanstack/react-router'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import TokenUsagePanel from './TokenUsagePanel'
import { formatDuration, formatRunDuration } from '../utils/stateHelpers'

const LockStatusBadge = ({ lockStatus, lockHolderIssue }) => {
  if (lockStatus === 'holding_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-green-600 bg-white/20 dark:text-green-400 dark:bg-green-900/20 border border-green-700/30 px-2 py-0.5 rounded">
        <Lock className="w-3 h-3" />
        <span>Holding Lock</span>
      </div>
    )
  } else if (lockStatus === 'waiting_for_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-yellow-400 bg-white/20 dark:text-yellow-400 dark:bg-yellow-900/20 border border-yellow-700/30 px-2 py-0.5 rounded">
        <Clock className="w-3 h-3" />
        <span>Waiting (#{lockHolderIssue})</span>
      </div>
    )
  } else if (lockStatus === 'no_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-blue-400 bg-white/20 dark:text-blue-400 dark:bg-blue-900/20 border border-blue-700/30 px-2 py-0.5 rounded">
        <Unlock className="w-3 h-3" />
        <span>No Lock</span>
      </div>
    )
  }
  return null
}

const PRIORITY_COLORS = {
  high: 'text-red-400 border-red-700/40 bg-white/20',
  medium: 'text-yellow-400 border-yellow-700/40 bg-white/20',
  low: 'text-blue-400 border-blue-700/40 bg-white/20',
}

function AnalysisBadge({ analysis, onClick }) {
  if (!analysis) return null
  const isSuccess = analysis.outcome === 'success'
  const totalRecs = (analysis.orchestratorRecommendations?.length || 0) + (analysis.projectRecommendations?.length || 0)
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1 text-xs px-2 py-0.5 rounded border transition-colors hover:opacity-80 ${
        isSuccess
          ? 'text-green-400 bg-white/20 border-green-700/30'
          : 'text-red-400 bg-white/20 border-red-700/30'
      }`}
      title="View pipeline analysis"
    >
      {isSuccess
        ? <CheckCircle2 className="w-3 h-3" />
        : <AlertCircle className="w-3 h-3" />}
      <span>{isSuccess ? 'Succeeded' : 'Failed'}</span>
      {totalRecs > 0 && <span className="opacity-70">· {totalRecs} rec{totalRecs !== 1 ? 's' : ''}</span>}
    </button>
  )
}

function AnalysisModal({ analysis, onClose }) {
  const backdropRef = useRef(null)

  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const handleBackdropClick = (e) => {
    if (e.target === backdropRef.current) onClose()
  }

  const orchRecs = analysis.orchestratorRecommendations || []
  const projRecs = analysis.projectRecommendations || []

  return (
    <div
      ref={backdropRef}
      onClick={handleBackdropClick}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
    >
      <div className="relative bg-gh-canvas border border-gh-border rounded-lg shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gh-border flex-shrink-0">
          <div className="flex items-center gap-2">
            {analysis.outcome === 'success'
              ? <CheckCircle2 className="w-4 h-4 text-green-400" />
              : <AlertCircle className="w-4 h-4 text-red-400" />}
            <span className="font-semibold text-sm">Pipeline Analysis</span>
            <span className={`text-xs px-1.5 py-0.5 rounded border ${
              analysis.outcome === 'success'
                ? 'text-green-400 bg-white/20 border-green-700/30'
                : 'text-red-400 bg-white/20 border-red-700/30'
            }`}>
              {analysis.outcome === 'success' ? 'Succeeded' : 'Failed'}
            </span>
          </div>
          <button onClick={onClose} className="text-gh-fg-muted hover:text-gh-fg transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="overflow-y-auto flex-1 px-5 py-4 space-y-5">
          {/* Summary */}
          {analysis.summary && (
            <div className="prose prose-invert prose-sm max-w-none text-gh-fg-muted text-xs leading-relaxed
              [&_h1]:text-gh-fg [&_h1]:text-sm [&_h1]:font-semibold [&_h1]:mb-1
              [&_h2]:text-gh-fg [&_h2]:text-xs [&_h2]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1
              [&_h3]:text-gh-fg [&_h3]:text-xs [&_h3]:font-semibold [&_h3]:mt-2 [&_h3]:mb-0.5
              [&_table]:border-collapse [&_table]:w-full [&_table]:text-xs
              [&_th]:text-left [&_th]:font-medium [&_th]:text-gh-fg-muted [&_th]:pb-1 [&_th]:border-b [&_th]:border-gh-border [&_th]:pr-4
              [&_td]:py-1 [&_td]:pr-4 [&_td]:border-b [&_td]:border-gh-border/40
              [&_code]:bg-gh-canvas-subtle [&_code]:px-1 [&_code]:rounded [&_code]:text-xs
              [&_ul]:list-disc [&_ul]:pl-4 [&_li]:my-0.5
              [&_strong]:text-gh-fg">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{analysis.summary}</ReactMarkdown>
            </div>
          )}

          {/* Recommendations */}
          {(orchRecs.length > 0 || projRecs.length > 0) && (
            <div className="space-y-3">
              {orchRecs.length > 0 && (
                <div>
                  <div className="text-xs font-semibold text-gh-fg mb-1.5">Orchestrator Recommendations</div>
                  <div className="space-y-1.5">
                    {orchRecs.map((rec, i) => (
                      <div key={i} className={`text-xs border rounded px-3 py-2 ${PRIORITY_COLORS[rec.priority] || 'text-gh-fg-muted border-gh-border bg-gh-canvas-subtle'}`}>
                        <div className="flex items-start gap-2">
                          <span className="uppercase font-semibold opacity-70 flex-shrink-0 text-[10px] mt-0.5">{rec.priority}</span>
                          <div>
                            <div>{rec.description}</div>
                            {rec.filePath && <div className="opacity-60 font-mono text-[10px] mt-0.5">{rec.filePath}</div>}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {projRecs.length > 0 && (
                <div>
                  <div className="text-xs font-semibold text-gh-fg mb-1.5">Project Recommendations</div>
                  <div className="space-y-1.5">
                    {projRecs.map((rec, i) => (
                      <div key={i} className={`text-xs border rounded px-3 py-2 ${PRIORITY_COLORS[rec.priority] || 'text-gh-fg-muted border-gh-border bg-gh-canvas-subtle'}`}>
                        <div className="flex items-start gap-2">
                          <span className="uppercase font-semibold opacity-70 flex-shrink-0 text-[10px] mt-0.5">{rec.priority}</span>
                          <div>{rec.description}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function PipelineRunHeader({
  pipelineRun,
  latestAgentExecutionId,
  isConversational,
  onKillRun,
  onDownloadDebugData,
  isFullscreen,
  onToggleFullscreen,
}) {
  const navigate = useNavigate()
  const [pipelineRunLogs, setPipelineRunLogs] = useState([])
  const [copied, setCopied] = useState(false)
  const [, setTick] = useState(0)
  const [analysis, setAnalysis] = useState(null)
  const [showAnalysisModal, setShowAnalysisModal] = useState(false)

  useEffect(() => {
    if (pipelineRun?.duration || pipelineRun?.status !== 'active') return
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [pipelineRun?.duration, pipelineRun?.status])

  useEffect(() => {
    if (!pipelineRun?.id) return
    let cancelled = false
    fetch(`/api/pipeline-run/${pipelineRun.id}/token-usage`)
      .then(r => r.json())
      .then(data => { if (!cancelled && data.success) setPipelineRunLogs(data.logs || []) })
      .catch(err => console.error('[PipelineRunHeader] token-usage fetch error:', err))
    return () => { cancelled = true }
  }, [pipelineRun?.id])

  useEffect(() => {
    if (!pipelineRun?.id) return
    let cancelled = false
    fetch(`/api/pipeline-run/${pipelineRun.id}/analysis`)
      .then(r => r.json())
      .then(data => { if (!cancelled && data.success && data.analysis) setAnalysis(data.analysis) })
      .catch(err => console.error('[PipelineRunHeader] analysis fetch error:', err))
    return () => { cancelled = true }
  }, [pipelineRun?.id])

  if (!pipelineRun) return null

  const repoUrl = pipelineRun.repo_url || null

  const getDuration = () => {
    if (pipelineRun.duration) return formatRunDuration(pipelineRun.duration)
    return formatDuration(pipelineRun.started_at)
  }

  const handleCopyId = async () => {
    try {
      await navigator.clipboard.writeText(pipelineRun.id)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard API unavailable or not in secure context
    }
  }

  const isActive = pipelineRun.status === 'active'
  const statusLabel = isActive
    ? 'Active'
    : (pipelineRun.outcome
      ? pipelineRun.outcome.charAt(0).toUpperCase() + pipelineRun.outcome.slice(1)
      : 'Completed')

  const metaCols = [
    {
      label: 'Project',
      value: repoUrl
        ? <a href={repoUrl} target="_blank" rel="noopener noreferrer" className="text-gh-accent-fg hover:underline">{pipelineRun.project}</a>
        : pipelineRun.project,
    },
    {
      label: 'Issue',
      value: pipelineRun.issue_url
        ? <a href={pipelineRun.issue_url} target="_blank" rel="noopener noreferrer" className="text-gh-accent-fg hover:underline">#{pipelineRun.issue_number}</a>
        : `#${pipelineRun.issue_number}`,
    },
    {
      label: 'Board',
      value: pipelineRun.board_url
        ? <a href={pipelineRun.board_url} target="_blank" rel="noopener noreferrer" className="text-gh-accent-fg hover:underline">{pipelineRun.board}</a>
        : pipelineRun.board,
    },
    {
      label: 'Status',
      value: (
        <div className="flex items-center gap-1.5">
          <div className={`w-2 h-2 rounded-full flex-shrink-0 ${isActive ? 'bg-green-500 animate-pulse' : 'bg-gray-500'}`} />
          <span className={isActive ? 'text-green-400' : ''}>{statusLabel}</span>
        </div>
      ),
    },
    {
      label: 'ID',
      value: (
        <span className="flex items-center gap-1 font-mono">
          <span>{pipelineRun.id}</span>
          <button
            onClick={handleCopyId}
            className="text-gh-fg-muted hover:text-gh-fg transition-colors"
            title={copied ? 'Copied!' : 'Copy ID'}
          >
            <Copy className="w-3 h-3" />
          </button>
        </span>
      ),
    },
    {
      label: 'Started',
      value: new Date(pipelineRun.started_at).toLocaleString(),
    },
    {
      label: 'Duration',
      value: getDuration(),
    },
  ]

  return (
    <div className="mb-2 flex-shrink-0">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 mb-2">
            <h2 className="text-xl font-semibold">{pipelineRun.issue_title}</h2>
            {pipelineRun.lock_status && (
              <LockStatusBadge
                lockStatus={pipelineRun.lock_status}
                lockHolderIssue={pipelineRun.lock_holder_issue}
              />
            )}
            <AnalysisBadge analysis={analysis} onClick={() => setShowAnalysisModal(true)} />
            <div className="flex divide-x divide-gh-border border border-gh-border rounded overflow-hidden flex-shrink-0 ml-auto">
              {onToggleFullscreen && (
                <button
                  onClick={onToggleFullscreen}
                  className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gh-canvas hover:bg-gh-border-muted transition-colors whitespace-nowrap"
                  title={isFullscreen ? 'Exit fullscreen' : 'Expand to fullscreen'}
                >
                  {isFullscreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
                </button>
              )}
              {latestAgentExecutionId && (
                <button
                  onClick={() => navigate({
                    to: '/agent-execution/$executionId',
                    params: { executionId: latestAgentExecutionId },
                    search: { autoAdvance: true },
                  })}
                  className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gh-canvas hover:bg-gh-border-muted transition-colors whitespace-nowrap"
                >
                  <ArrowRight className="w-4 h-4" />
                  <span>View Execution</span>
                </button>
              )}
              {pipelineRun.status === 'active' && (
                <button
                  onClick={onKillRun}
                  className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gh-canvas hover:bg-gh-border-muted transition-colors whitespace-nowrap text-red-400"
                  title="Kill this pipeline run"
                >
                  <XCircle className="w-4 h-4" />
                  Kill Run
                </button>
              )}
              <button
                onClick={onDownloadDebugData}
                className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gh-canvas hover:bg-gh-border-muted transition-colors whitespace-nowrap"
                title="Download debug data as JSON"
              >
                📥 Download
              </button>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="text-xs border-collapse">
              <thead>
                <tr>
                  {metaCols.map(col => (
                    <th key={col.label} className="text-left font-medium pr-5 pb-0.5 whitespace-nowrap text-gh-fg-muted opacity-70">{col.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  {metaCols.map(col => (
                    <td key={col.label} className="pr-5 whitespace-nowrap text-gh-fg-muted">{col.value}</td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>
        </div>

      </div>

      <div className="">
        <TokenUsagePanel logs={pipelineRunLogs} />
      </div>

      {pipelineRun.lock_status === 'waiting_for_lock' && pipelineRun.blocked_by_issue && (
        <div className="mt-2 text-xs text-yellow-400 bg-white/20 border border-yellow-700/20 px-3 py-2 rounded">
          ⚠️ This pipeline is waiting for lock currently held by issue #{pipelineRun.blocked_by_issue}
        </div>
      )}

      {isConversational && (
        <div className="mt-2 bg-white/20 border border-blue-800 text-blue-400 px-4 py-3 rounded-md flex items-center gap-3">
          <MessageSquare className="w-5 h-5 animate-pulse" />
          <div>
            <div className="font-semibold">Conversational Loop Active</div>
            <div className="text-xs opacity-80">Waiting for human feedback on GitHub...</div>
          </div>
        </div>
      )}

      {showAnalysisModal && analysis && (
        <AnalysisModal analysis={analysis} onClose={() => setShowAnalysisModal(false)} />
      )}
    </div>
  )
}
