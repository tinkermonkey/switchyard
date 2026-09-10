import { CheckCircle, XCircle, Clock, AlertCircle, RefreshCw } from 'lucide-react'

export default function DevContainerStatus({ devContainer }) {
  const getStatusBadge = (status) => {
    switch (status) {
      case 'verified':
        return (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-green-500/10 text-green-500 border border-green-500/20">
            <CheckCircle className="w-3 h-3 mr-1" />
            Verified
          </span>
        )
      case 'in_progress':
        return (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-blue-500/10 text-blue-500 border border-blue-500/20">
            <Clock className="w-3 h-3 mr-1" />
            In Progress
          </span>
        )
      case 'blocked':
        return (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-red-500/10 text-red-500 border border-red-500/20">
            <XCircle className="w-3 h-3 mr-1" />
            Blocked
          </span>
        )
      case 'unverified':
        return (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-yellow-500/10 text-yellow-500 border border-yellow-500/20">
            <AlertCircle className="w-3 h-3 mr-1" />
            Unverified
          </span>
        )
      case 'changes_needed':
        // Brief/transient in practice: the repair cycle's env-rebuild sub-cycle
        // resets this to 'unverified' and retries automatically within ~30s.
        return (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-orange-500/10 text-orange-500 border border-orange-500/20">
            <RefreshCw className="w-3 h-3 mr-1" />
            Retrying
          </span>
        )
      default:
        return (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-gray-500/10 text-gray-400 border border-gray-500/20">
            {status}
          </span>
        )
    }
  }

  const formatTimestamp = (timestamp) => {
    if (!timestamp) return 'Never'
    try {
      const date = new Date(timestamp)
      return date.toLocaleString()
    } catch {
      return timestamp
    }
  }

  return (
    <div className="bg-gh-canvas border border-gh-border rounded-md p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-gh-fg-muted">Dev Container</span>
        {getStatusBadge(devContainer?.status)}
      </div>
      {devContainer?.image_name && (
        <p className="text-xs text-gh-fg-muted font-mono">
          {devContainer.image_name}
        </p>
      )}
      {devContainer?.updated_at && (
        <p className="text-xs text-gh-fg-muted mt-1">
          Updated: {formatTimestamp(devContainer.updated_at)}
        </p>
      )}
      {devContainer?.pending_operation && (
        // The status badge above is the IMAGE's state and does not move while a
        // requested operation waits for the dev_container_build lock, so the wait
        // needs its own line -- otherwise a queued rebuild is indistinguishable
        // from one that already finished.
        <p className="text-xs text-blue-500 mt-2">
          {devContainer.pending_operation} queued since{' '}
          {formatTimestamp(devContainer.pending_operation_at)}
        </p>
      )}
      {devContainer?.error_message && (
        <p className="text-xs text-red-500 mt-2">
          {devContainer.error_message}
        </p>
      )}
      {devContainer?.last_operation_error && (
        // A requested operation that never ran at all. Not a verdict on the image
        // (the badge above is), so it is reported separately rather than as a
        // status. Dated like the queued line above: the record survives until
        // something writes a new status, so an operator has to be able to tell
        // whether it predates the badge.
        <p className="text-xs text-orange-500 mt-2">
          {devContainer.last_operation_error}
          {devContainer.last_operation_error_at &&
            ` (${formatTimestamp(devContainer.last_operation_error_at)})`}
        </p>
      )}
    </div>
  )
}
