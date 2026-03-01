import { useState, useEffect } from 'react'
import { Lock, Unlock, Clock, XCircle, ArrowRight, MessageSquare, Copy, Maximize2, Minimize2 } from 'lucide-react'
import { useNavigate } from '@tanstack/react-router'
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
      <div className="flex items-center gap-1 text-xs text-yellow-400 bg-yellow-900/20 dark:text-yellow-400 dark:bg-yellow-900/20 border border-yellow-700/30 px-2 py-0.5 rounded">
        <Clock className="w-3 h-3" />
        <span>Waiting (#{lockHolderIssue})</span>
      </div>
    )
  } else if (lockStatus === 'no_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-blue-400 bg-blue-900/20 dark:text-blue-400 dark:bg-blue-900/20 border border-blue-700/30 px-2 py-0.5 rounded">
        <Unlock className="w-3 h-3" />
        <span>No Lock</span>
      </div>
    )
  }
  return null
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

  useEffect(() => {
    if (!pipelineRun?.id) return
    let cancelled = false
    fetch(`/api/pipeline-run/${pipelineRun.id}/token-usage`)
      .then(r => r.json())
      .then(data => { if (!cancelled && data.success) setPipelineRunLogs(data.logs || []) })
      .catch(err => console.error('[PipelineRunHeader] token-usage fetch error:', err))
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

        <div className="flex divide-x divide-gh-border border border-gh-border rounded overflow-hidden flex-shrink-0">
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
          {onToggleFullscreen && (
            <button
              onClick={onToggleFullscreen}
              className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gh-canvas hover:bg-gh-border-muted transition-colors whitespace-nowrap"
              title={isFullscreen ? 'Exit fullscreen' : 'Expand to fullscreen'}
            >
              {isFullscreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
            </button>
          )}
        </div>
      </div>

      <div className="">
        <TokenUsagePanel logs={pipelineRunLogs} />
      </div>

      {pipelineRun.lock_status === 'waiting_for_lock' && pipelineRun.blocked_by_issue && (
        <div className="mt-2 text-xs text-yellow-400 bg-yellow-900/10 border border-yellow-700/20 px-3 py-2 rounded">
          ⚠️ This pipeline is waiting for lock currently held by issue #{pipelineRun.blocked_by_issue}
        </div>
      )}

      {isConversational && (
        <div className="mt-2 bg-blue-900/20 border border-blue-800 text-blue-400 px-4 py-3 rounded-md flex items-center gap-3">
          <MessageSquare className="w-5 h-5 animate-pulse" />
          <div>
            <div className="font-semibold">Conversational Loop Active</div>
            <div className="text-xs opacity-80">Waiting for human feedback on GitHub...</div>
          </div>
        </div>
      )}
    </div>
  )
}
