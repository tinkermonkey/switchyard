import { useState, useEffect } from 'react'
import { Link } from '@tanstack/react-router'
import { Activity, GitBranch, Sparkles, FolderGit2, CheckCircle, XCircle, Clock, AlertCircle, GitCommit, FilePlus, FileMinus } from 'lucide-react'
import Header from './Header'

export default function Projects() {
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchProjects()
    // Refresh every 30 seconds
    const interval = setInterval(fetchProjects, 30000)
    return () => clearInterval(interval)
  }, [])

  const fetchProjects = async () => {
    try {
      const response = await fetch('/api/projects')
      const data = await response.json()

      if (data.success) {
        setProjects(data.projects)
        setError(null)
      } else {
        setError(data.error)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

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
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />

      <div className="flex gap-3 my-3">
        <Link
          to="/"
          activeProps={{
            className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
          }}
          inactiveProps={{
            className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          }}
        >
          <Activity className="inline w-4 h-4 mr-2" />
          Dashboard
        </Link>
        <Link
          to="/pipeline"
          activeProps={{
            className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
          }}
          inactiveProps={{
            className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          }}
        >
          <GitBranch className="inline w-4 h-4 mr-2" />
          Pipeline View
        </Link>
        <Link
          to="/review-learning"
          activeProps={{
            className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
          }}
          inactiveProps={{
            className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          }}
        >
          <Sparkles className="inline w-4 h-4 mr-2" />
          Review Learning
        </Link>
        <Link
          to="/projects"
          activeProps={{
            className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
          }}
          inactiveProps={{
            className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          }}
        >
          <FolderGit2 className="inline w-4 h-4 mr-2" />
          Projects
        </Link>
      </div>

      <div className="mt-6">
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg shadow-sm">
          <div className="px-6 py-4 border-b border-gh-border">
            <h2 className="text-xl font-semibold text-gh-fg">Configured Projects</h2>
            <p className="text-sm text-gh-fg-muted mt-1">
              {loading ? 'Loading...' : `${projects.length} project${projects.length !== 1 ? 's' : ''} configured`}
            </p>
          </div>

          {error && (
            <div className="px-6 py-4 bg-red-500/10 border-b border-red-500/20">
              <p className="text-sm text-red-500">Error: {error}</p>
            </div>
          )}

          {loading ? (
            <div className="px-6 py-8 text-center">
              <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
              <p className="mt-2 text-sm text-gh-fg-muted">Loading projects...</p>
            </div>
          ) : (
            <div className="divide-y divide-gh-border">
              {projects.map((project) => (
                <div key={project.name} className="px-6 py-4 hover:bg-gh-canvas-inset transition-colors">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3">
                        <h3 className="text-lg font-semibold text-gh-fg">{project.name}</h3>
                        {project.github?.url && (
                          <a
                            href={project.github.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-sm text-gh-accent-fg hover:underline"
                          >
                            {project.github.org}/{project.github.repo}
                          </a>
                        )}
                      </div>

                      <div className="mt-3 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {/* Dev Container Status */}
                        <div className="bg-gh-canvas-inset border border-gh-border rounded-md p-3">
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-xs font-medium text-gh-fg-muted">Dev Container</span>
                            {getStatusBadge(project.dev_container?.status)}
                          </div>
                          {project.dev_container?.image_name && (
                            <p className="text-xs text-gh-fg-muted font-mono">
                              {project.dev_container.image_name}
                            </p>
                          )}
                          {project.dev_container?.updated_at && (
                            <p className="text-xs text-gh-fg-muted mt-1">
                              Updated: {formatTimestamp(project.dev_container.updated_at)}
                            </p>
                          )}
                          {project.dev_container?.error_message && (
                            <p className="text-xs text-red-500 mt-2">
                              {project.dev_container.error_message}
                            </p>
                          )}
                        </div>

                        {/* Workspace Status - now spans full width */}
                        <div className="bg-gh-canvas-inset border border-gh-border rounded-md p-3 md:col-span-2 lg:col-span-3">
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-xs font-medium text-gh-fg-muted">Workspace</span>
                            {project.workspace?.exists ? (
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
                            {project.workspace?.path}
                          </p>

                          {/* Git Branches */}
                          {project.workspace?.git_branches && (
                            <div className="mt-3 border-t border-gh-border pt-3">
                              <div className="flex items-center justify-between mb-2">
                                <span className="text-xs font-medium text-gh-fg-muted">
                                  Git Branches
                                  {project.workspace.git_branches.current_branch && (
                                    <span className="ml-2 text-gh-accent-fg">
                                      (current: {project.workspace.git_branches.current_branch})
                                    </span>
                                  )}
                                </span>
                                <span className="text-xs text-gh-fg-muted">
                                  Updated: {formatTimestamp(project.workspace.git_branches.collected_at)}
                                </span>
                              </div>

                              <div className="space-y-2">
                                {project.workspace.git_branches.branches.map((branch) => (
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

                                {project.workspace.git_branches.branches.length === 0 && (
                                  <p className="text-xs text-gh-fg-muted">No branches found</p>
                                )}
                              </div>
                            </div>
                          )}
                        </div>

                        {/* Pipelines */}
                        <div className="bg-gh-canvas-inset border border-gh-border rounded-md p-3">
                          <span className="text-xs font-medium text-gh-fg-muted block mb-2">Pipelines</span>
                          {project.pipelines && project.pipelines.length > 0 ? (
                            <div className="flex flex-wrap gap-1">
                              {project.pipelines.map((pipeline) => (
                                <span
                                  key={pipeline}
                                  className="inline-block px-2 py-1 rounded-md text-xs bg-gh-accent-emphasis/10 text-gh-accent-fg border border-gh-accent-emphasis/20"
                                >
                                  {pipeline}
                                </span>
                              ))}
                            </div>
                          ) : (
                            <p className="text-xs text-gh-fg-muted">No pipelines configured</p>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              ))}

              {projects.length === 0 && !loading && (
                <div className="px-6 py-8 text-center">
                  <p className="text-sm text-gh-fg-muted">No projects configured</p>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
