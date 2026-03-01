import { useState, useEffect } from 'react'
import { Lock, Unlock, Clock, XCircle, ArrowRight, MessageSquare } from 'lucide-react'
import { useNavigate } from '@tanstack/react-router'
import TokenUsagePanel from './TokenUsagePanel'

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
}) {
  const navigate = useNavigate()
  const [pipelineRunLogs, setPipelineRunLogs] = useState([])

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

  return (
    <div className="mb-4 flex-shrink-0">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-semibold">{pipelineRun.issue_title}</h2>
            {pipelineRun.lock_status && (
              <LockStatusBadge
                lockStatus={pipelineRun.lock_status}
                lockHolderIssue={pipelineRun.lock_holder_issue}
              />
            )}
          </div>
          <p className="text-sm text-gh-fg-muted mt-1">
            {pipelineRun.project} • Issue #{pipelineRun.issue_number} • Board: {pipelineRun.board}
          </p>
          <p className="text-sm text-gh-fg-muted">
            Started: {new Date(pipelineRun.started_at).toLocaleString()}
            {pipelineRun.ended_at && ` • Ended: ${new Date(pipelineRun.ended_at).toLocaleString()}`}
          </p>
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
        </div>
      </div>

      <TokenUsagePanel logs={pipelineRunLogs} />

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
