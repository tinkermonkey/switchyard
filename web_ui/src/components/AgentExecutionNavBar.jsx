import { Link } from '@tanstack/react-router'
import { ChevronLeft, ChevronRight, ArrowLeft } from 'lucide-react'

const formatAgentName = (agentName) => {
  if (!agentName || typeof agentName !== 'string' || agentName.trim() === '') return 'Unknown Agent'
  return agentName.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
}

export default function AgentExecutionNavBar({
  pipelineRunId,
  pipelineExecutions,
  currentExecutionIndex,
  executionData,
  autoAdvance,
  onPrevious,
  onNext,
  onAutoAdvanceToggle,
}) {
  if (pipelineExecutions.length === 0 || currentExecutionIndex < 0) return null

  return (
    <div className="mt-3 mb-3 p-3 bg-gh-canvas-subtle rounded-md border border-gh-border">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {/* Back to Pipeline Runs link */}
          <Link
            to="/pipeline-run"
            search={{ runId: pipelineRunId }}
            className="flex items-center gap-1.5 px-3 py-2 bg-gh-canvas border border-gh-border rounded hover:bg-gh-border-muted transition-colors text-sm"
            title="Back to Pipeline Runs"
          >
            <ArrowLeft className="w-4 h-4" />
            <span>Pipeline Runs</span>
          </Link>

          {/* Navigation buttons */}
          <div className="flex gap-1 pl-3 border-l border-gh-border">
            <button
              onClick={onPrevious}
              disabled={currentExecutionIndex === 0}
              className="p-2 bg-gh-canvas border border-gh-border rounded hover:bg-gh-border-muted transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
              title="Previous agent execution"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <button
              onClick={onNext}
              disabled={currentExecutionIndex === pipelineExecutions.length - 1}
              className="p-2 bg-gh-canvas border border-gh-border rounded hover:bg-gh-border-muted transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
              title="Next agent execution"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>

          {/* Execution counter and agent info */}
          <div className="flex items-center gap-2 pl-2 border-l border-gh-border">
            <span className="text-sm font-mono font-semibold">
              {currentExecutionIndex + 1} / {pipelineExecutions.length}
            </span>
            <span className="text-gh-fg-muted">·</span>
            <span className="text-sm font-medium">
              {formatAgentName(executionData.agent)}
            </span>
            <span
              className={`text-xs px-2 py-0.5 rounded-full ${
                executionData.status === 'completed'
                  ? 'bg-green-900/30 text-green-400'
                  : executionData.status === 'failed'
                  ? 'bg-red-900/30 text-red-400'
                  : 'bg-blue-900/30 text-blue-400'
              }`}
            >
              {executionData.status}
            </span>
          </div>

          {/* Auto-advance checkbox */}
          <label className="flex items-center gap-2 cursor-pointer pl-2 border-l border-gh-border">
            <input
              type="checkbox"
              checked={autoAdvance}
              onChange={onAutoAdvanceToggle}
              className="w-4 h-4 rounded border-gh-border bg-gh-canvas text-gh-accent-emphasis focus:ring-2 focus:ring-gh-accent-emphasis focus:ring-offset-0"
            />
            <span className="text-sm">Auto-advance</span>
          </label>
        </div>
      </div>
    </div>
  )
}
