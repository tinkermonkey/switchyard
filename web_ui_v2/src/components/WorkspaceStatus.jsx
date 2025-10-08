import { CheckCircle, XCircle, GitBranch, FilePlus, FileMinus, GitCommit } from 'lucide-react'

export default function WorkspaceStatus({ workspace }) {
  const formatTimestamp = (timestamp) => {
    if (!timestamp) return 'Never'
    try {
      const date = new Date(timestamp)
      return date.toLocaleString()
    } catch {
      return timestamp
    }
  }

  const getChangeTypeColor = (changeType) => {
    switch (changeType) {
      case 'added':
        return 'text-green-500'
      case 'deleted':
        return 'text-red-500'
      case 'modified':
        return 'text-yellow-500'
      default:
        return 'text-gh-fg-muted'
    }
  }

  const getChangeTypeIcon = (changeType) => {
    switch (changeType) {
      case 'added':
        return <FilePlus className="w-3 h-3" />
      case 'deleted':
        return <FileMinus className="w-3 h-3" />
      default:
        return <GitCommit className="w-3 h-3" />
    }
  }

  return (
    <div className="bg-gh-canvas border border-gh-border rounded-md p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-gh-fg-muted">Workspace</span>
        {workspace?.exists ? (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-green-500/10 text-green-500">
            <CheckCircle className="w-3 h-3 mr-1" />
            Exists
          </span>
        ) : (
          <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-gray-500/10 text-gray-400">
            <XCircle className="w-3 h-3 mr-1" />
            Not Found
          </span>
        )}
      </div>
      <p className="text-xs text-gh-fg-muted font-mono mb-3">
        {workspace?.path}
      </p>

      {/* Git Branches */}
      {workspace?.git_branches && (
        <div className="mt-3 border-t border-gh-border pt-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-gh-fg-muted">
              Git Branches
              {workspace.git_branches.current_branch && (
                <span className="ml-2 text-gh-accent-fg">
                  (current: {workspace.git_branches.current_branch})
                </span>
              )}
            </span>
            <span className="text-xs text-gh-fg-muted">
              Updated: {formatTimestamp(workspace.git_branches.collected_at)}
            </span>
          </div>

          <div className="space-y-2">
            {workspace.git_branches.branches.map((branch) => (
              <div
                key={branch.name}
                className={`border rounded-md p-2 ${
                  branch.is_current
                    ? 'border-gh-accent-emphasis bg-gh-accent-emphasis/5'
                    : 'border-gh-border bg-gh-canvas'
                }`}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <GitBranch className="w-3 h-3 text-gh-fg-muted" />
                      <span className="text-xs font-mono font-medium text-gh-fg">
                        {branch.name}
                      </span>
                      {branch.is_current && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-gh-accent-emphasis text-white">
                          current
                        </span>
                      )}
                      {branch.tracking_branch && (
                        <span className="text-xs text-gh-fg-muted">
                          → {branch.tracking_branch}
                        </span>
                      )}
                    </div>

                    {branch.file_changes && branch.file_changes.length > 0 && (
                      <div className="mt-2 ml-5">
                        <div className="text-[10px] text-gh-fg-muted mb-1">
                          {branch.total_files_changed} file{branch.total_files_changed !== 1 ? 's' : ''} changed
                        </div>
                        <div className="space-y-1 max-h-40 overflow-y-auto">
                          {branch.file_changes.map((change, idx) => (
                            <div
                              key={idx}
                              className={`flex items-center gap-2 text-[11px] font-mono ${getChangeTypeColor(change.change_type)}`}
                            >
                              {getChangeTypeIcon(change.change_type)}
                              <span className="flex-1">{change.file}</span>
                              {(change.insertions > 0 || change.deletions > 0) && (
                                <span className="text-[10px]">
                                  {change.insertions > 0 && (
                                    <span className="text-green-500">+{change.insertions}</span>
                                  )}
                                  {change.insertions > 0 && change.deletions > 0 && ' '}
                                  {change.deletions > 0 && (
                                    <span className="text-red-500">-{change.deletions}</span>
                                  )}
                                </span>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}

            {workspace.git_branches.branches.length === 0 && (
              <p className="text-xs text-gh-fg-muted">No branches found</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
