import { Lock, Unlock, Clock, XCircle, ArrowRight, MessageSquare } from 'lucide-react'
import { useNavigate } from '@tanstack/react-router'

const LockStatusBadge = ({ lockStatus, lockHolderIssue }) => {
  if (lockStatus === 'holding_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-green-400 bg-green-900/20 border border-green-700/30 px-2 py-0.5 rounded">
        <Lock className="w-3 h-3" />
        <span>Holding Lock</span>
      </div>
    )
  } else if (lockStatus === 'waiting_for_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-yellow-400 bg-yellow-900/20 border border-yellow-700/30 px-2 py-0.5 rounded">
        <Clock className="w-3 h-3" />
        <span>Waiting (#{lockHolderIssue})</span>
      </div>
    )
  } else if (lockStatus === 'no_lock') {
    return (
      <div className="flex items-center gap-1 text-xs text-blue-400 bg-blue-900/20 border border-blue-700/30 px-2 py-0.5 rounded">
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

        <div className="flex gap-2 flex-shrink-0">
          {latestAgentExecutionId && (
            <button
              onClick={() => navigate({
                to: '/agent-execution/$executionId',
                params: { executionId: latestAgentExecutionId },
                search: { autoAdvance: true },
              })}
              className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gh-canvas border border-gh-border rounded hover:bg-gh-border-muted transition-colors whitespace-nowrap"
            >
              <span>View Latest Agent Execution</span>
              <ArrowRight className="w-4 h-4" />
            </button>
          )}
          {pipelineRun.status === 'active' && (
            <button
              onClick={onKillRun}
              className="px-3 py-1.5 text-sm bg-red-900/20 border border-red-800 text-red-400 rounded hover:bg-red-900/40 transition-colors whitespace-nowrap flex items-center gap-1"
              title="Kill this pipeline run"
            >
              <XCircle className="w-4 h-4" />
              Kill Run
            </button>
          )}
          <button
            onClick={onDownloadDebugData}
            className="px-3 py-1.5 text-sm bg-gh-canvas-subtle border border-gh-border rounded hover:bg-gh-border-muted transition-colors whitespace-nowrap"
            title="Download debug data as JSON"
          >
            📥 Download
          </button>
        </div>
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
