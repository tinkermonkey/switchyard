import { useState, useEffect } from 'react'
import { ChevronDown, ChevronRight, Clock, AlertCircle, ExternalLink } from 'lucide-react'

export default function ClaudeClusterView({ cluster }) {
  const [expanded, setExpanded] = useState(false)
  const [failureDetails, setFailureDetails] = useState(null)
  const [loading, setLoading] = useState(false)

  const formatTimestamp = (timestamp) => {
    return new Date(timestamp).toLocaleString()
  }

  const formatDuration = (seconds) => {
    if (seconds < 60) return `${Math.round(seconds)}s`
    const minutes = Math.floor(seconds / 60)
    const remainingSeconds = Math.round(seconds % 60)
    return `${minutes}m ${remainingSeconds}s`
  }

  // Fetch failure details when expanded
  useEffect(() => {
    if (expanded && !failureDetails && cluster.cluster_id) {
      setLoading(true)
      // Fetch failure details from Elasticsearch via new API endpoint
      fetch(`/api/medic/claude/clusters/${encodeURIComponent(cluster.cluster_id)}/failures`)
        .then(res => res.json())
        .then(data => {
          if (data.failures) {
            setFailureDetails(data.failures)
          }
        })
        .catch(err => console.error('Failed to fetch failure details:', err))
        .finally(() => setLoading(false))
    }
  }, [expanded, cluster.cluster_id, failureDetails])

  return (
    <div className="bg-gh-canvas border border-gh-border rounded-lg overflow-hidden">
      {/* Cluster Header */}
      <div
        className="p-4 cursor-pointer hover:bg-gh-canvas-subtle transition-colors flex items-start justify-between"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            {expanded ? (
              <ChevronDown className="w-4 h-4 text-gh-fg-muted" />
            ) : (
              <ChevronRight className="w-4 h-4 text-gh-fg-muted" />
            )}
            <span className="text-sm font-semibold text-gh-fg">
              Cluster - {formatTimestamp(cluster.timestamp)}
            </span>
            <span className="px-2 py-0.5 rounded text-xs bg-orange-500/10 text-orange-500">
              {cluster.failure_count} failures
            </span>
            <span className="px-2 py-0.5 rounded text-xs bg-blue-500/10 text-blue-500">
              {formatDuration(cluster.duration_seconds)}
            </span>
          </div>

          <div className="flex items-center gap-4 text-xs text-gh-fg-muted ml-6">
            <div className="flex items-center gap-1">
              <span>Session: {cluster.session_id?.slice(0, 8)}...</span>
            </div>
            {cluster.task_id && (
              <div className="flex items-center gap-1">
                <span>Task: {cluster.task_id.slice(0, 30)}...</span>
              </div>
            )}
          </div>

          {cluster.tools_attempted && cluster.tools_attempted.length > 0 && (
            <div className="text-xs text-gh-fg-muted ml-6 mt-1">
              Tools: {cluster.tools_attempted.join(', ')}
            </div>
          )}
        </div>
      </div>

      {/* Cluster Details (Expanded) */}
      {expanded && (
        <div className="border-t border-gh-border bg-gh-canvas-subtle p-4">
          {loading && (
            <div className="flex items-center justify-center p-4">
              <div className="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-gh-accent-emphasis"></div>
            </div>
          )}

          {!loading && failureDetails && failureDetails.length > 0 && (
            <div>
              <h5 className="text-xs font-semibold text-gh-fg mb-3">Failure Details</h5>
              <div className="space-y-3">
                {failureDetails.map((failure, idx) => (
                  <DetailedFailureItem key={idx} failure={failure} index={idx + 1} />
                ))}
              </div>
            </div>
          )}

          {!loading && failureDetails && failureDetails.length === 0 && (
            <p className="text-xs text-gh-fg-muted">No detailed failure information available</p>
          )}

          {/* Link to Agent Execution View */}
          {cluster.task_id && (
            <div className="mt-4 pt-4 border-t border-gh-border">
              <a
                href={`/?task_id=${cluster.task_id}`}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 text-xs text-gh-accent-fg hover:underline"
              >
                <ExternalLink className="w-3 h-3" />
                View Full Agent Execution
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function DetailedFailureItem({ failure, index }) {
  const formatTime = (timestamp) => {
    return new Date(timestamp).toLocaleTimeString()
  }

  // Extract tool parameters from raw_event
  const getToolParams = () => {
    try {
      const content = failure.raw_event?.event?.message?.content
      if (Array.isArray(content)) {
        const toolResult = content.find(c => c.type === 'tool_result')
        if (toolResult) {
          return toolResult.tool_use_id
        }
      }
    } catch (e) {
      return null
    }
  }

  // Extract error message
  const getErrorMessage = () => {
    try {
      const content = failure.raw_event?.event?.message?.content
      if (Array.isArray(content)) {
        const toolResult = content.find(c => c.type === 'tool_result')
        if (toolResult && toolResult.content) {
          return toolResult.content
        }
      }
    } catch (e) {
      return failure.error_message || 'Unknown error'
    }
    return failure.error_message || 'Unknown error'
  }

  return (
    <div className="bg-gh-canvas border border-gh-border rounded-lg p-3">
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="px-2 py-0.5 rounded text-xs bg-gh-canvas-subtle border border-gh-border font-mono">
            #{index}
          </span>
          <span className="text-sm font-semibold text-gh-fg">{failure.tool_name}</span>
          <span className="px-2 py-0.5 rounded text-xs bg-red-500/10 text-red-500 border border-red-500/20">
            Failed
          </span>
        </div>
        <span className="text-xs text-gh-fg-muted font-mono">{formatTime(failure.timestamp)}</span>
      </div>

      {/* Tool Parameters */}
      {failure.tool_params && Object.keys(failure.tool_params).length > 0 && (
        <div className="mb-2">
          <span className="text-xs font-semibold text-gh-fg-muted mb-1 block">Parameters:</span>
          <div className="bg-gh-canvas-subtle border border-gh-border rounded p-2">
            <pre className="text-xs text-gh-fg font-mono whitespace-pre-wrap overflow-x-auto">
              {JSON.stringify(failure.tool_params, null, 2)}
            </pre>
          </div>
        </div>
      )}

      {/* Error Message */}
      <div>
        <span className="text-xs font-semibold text-gh-fg-muted mb-1 block">Error:</span>
        <div className="bg-red-500/10 border border-red-500/20 rounded p-2">
          <pre className="text-xs text-red-500 font-mono whitespace-pre-wrap overflow-x-auto">
            {getErrorMessage()}
          </pre>
        </div>
      </div>
    </div>
  )
}
