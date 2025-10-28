import React, { useState, useEffect } from 'react'
import { AlertTriangle, CheckCircle, XCircle, Loader, Wrench, FileText, Trash2 } from 'lucide-react'
import { useSocket } from '../contexts/SocketContext'
import { formatDuration } from '../utils/stateHelpers'
import { mergeArrayByIdStable, mergeObjectMapStable } from '../utils/eventMerging'

/**
 * RepairCycleContainers - Display active repair cycle containers with progress tracking
 */
const RepairCycleContainers = () => {
  const { events } = useSocket()
  const [containers, setContainers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [expandedContainer, setExpandedContainer] = useState(null)
  const [containerLogs, setContainerLogs] = useState({})
  const [loadingLogs, setLoadingLogs] = useState({})
  const [killingContainer, setKillingContainer] = useState({})

  // Fetch containers on mount and periodically
  const fetchContainers = async (isInitialLoad = false) => {
    try {
      // Only show loading spinner on initial load, not on background refreshes
      if (isInitialLoad) {
        setLoading(true)
      }
      const response = await fetch('http://localhost:5001/api/repair-cycle-containers')
      const data = await response.json()

      if (data.success) {
        // Use stable merge to prevent unnecessary re-renders
        // Containers might not have an 'id' field, so we'll use 'container_name' as the key
        setContainers(current => mergeArrayByIdStable(current, data.containers, 'container_name'))
        setError(null)
      } else {
        setError(data.error || 'Failed to fetch containers')
      }
    } catch (err) {
      setError(`Error fetching containers: ${err.message}`)
    } finally {
      if (isInitialLoad) {
        setLoading(false)
      }
    }
  }

  useEffect(() => {
    fetchContainers(true) // Pass true for initial load

    // Refresh every 5 seconds (background refresh, no loading spinner)
    const interval = setInterval(() => fetchContainers(false), 5000)

    return () => clearInterval(interval)
  }, [])

  // Listen for WebSocket events
  useEffect(() => {
    if (!events || events.length === 0) return

    const latestEvent = events[events.length - 1]

    if (latestEvent.event_type?.startsWith('repair_cycle_container_')) {
      // Refresh containers when any repair cycle event occurs (background refresh)
      fetchContainers(false)
    }
  }, [events])

  // Fetch logs for a specific container
  const fetchLogs = async (project, issueNumber) => {
    const key = `${project}-${issueNumber}`
    setLoadingLogs(prev => ({ ...prev, [key]: true }))
    
    try {
      const response = await fetch(`http://localhost:5001/api/repair-cycle-containers/${project}/${issueNumber}/logs`)
      const data = await response.json()
      
      if (data.success) {
        // Use stable merge for object map updates
        setContainerLogs(prev => mergeObjectMapStable(prev, { [key]: data.logs }))
      } else {
        setContainerLogs(prev => mergeObjectMapStable(prev, { [key]: `Error: ${data.error}` }))
      }
    } catch (err) {
      setContainerLogs(prev => mergeObjectMapStable(prev, { [key]: `Error fetching logs: ${err.message}` }))
    } finally {
      setLoadingLogs(prev => ({ ...prev, [key]: false }))
    }
  }

  // Kill a container
  const killContainer = async (project, issueNumber) => {
    const key = `${project}-${issueNumber}`
    
    if (!confirm(`Are you sure you want to kill the repair cycle container for ${project} #${issueNumber}?`)) {
      return
    }
    
    setKillingContainer(prev => ({ ...prev, [key]: true }))
    
    try {
      const response = await fetch(
        `http://localhost:5001/api/repair-cycle-containers/${project}/${issueNumber}/kill`,
        { method: 'POST' }
      )
      const data = await response.json()
      
      if (data.success) {
        // Refresh containers (background refresh after kill)
        await fetchContainers(false)
      } else {
        alert(`Failed to kill container: ${data.error}`)
      }
    } catch (err) {
      alert(`Error killing container: ${err.message}`)
    } finally {
      setKillingContainer(prev => ({ ...prev, [key]: false }))
    }
  }

  // Toggle expanded view
  const toggleExpanded = (containerName) => {
    if (expandedContainer === containerName) {
      setExpandedContainer(null)
    } else {
      setExpandedContainer(containerName)
      
      // Extract project and issue from container name
      const match = containerName.match(/repair-cycle-(.+)-(\d+)-/)
      if (match) {
        const [, project, issueNumber] = match
        const key = `${project}-${issueNumber}`
        
        // Fetch logs if not already loaded
        if (!containerLogs[key]) {
          fetchLogs(project, issueNumber)
        }
      }
    }
  }

  // Get status color based on checkpoint age and container age
  const getStatusColor = (container) => {
    if (container.is_finished) {
      return container.result?.overall_success ? 'bg-green-500' : 'bg-red-500'
    }
    
    const checkpoint = container.checkpoint
    if (!checkpoint) {
      // No checkpoint yet - starting up
      if (container.container_age_seconds < 300) {
        return 'bg-blue-500' // Young container, starting up
      } else {
        return 'bg-yellow-500' // Old container without checkpoint - warning
      }
    }
    
    const checkpointAge = checkpoint.checkpoint_age_seconds || 0
    if (checkpointAge < 300) {
      return 'bg-green-500' // Making progress
    } else if (checkpointAge < 600) {
      return 'bg-yellow-500' // Checkpoint getting stale
    } else {
      return 'bg-red-500' // Stale checkpoint - likely stuck
    }
  }

  // Get status icon
  const getStatusIcon = (container) => {
    if (container.is_finished) {
      return container.result?.overall_success ? 
        <CheckCircle className="w-5 h-5 text-green-500" /> : 
        <XCircle className="w-5 h-5 text-red-500" />
    }
    
    return <Loader className="w-5 h-5 text-blue-500 animate-spin" />
  }

  // Calculate progress percentage
  const getProgress = (container) => {
    const checkpoint = container.checkpoint
    if (!checkpoint) return 0
    
    const iteration = checkpoint.iteration || 0
    const maxIterations = 10 // Could come from config
    
    return Math.min(100, (iteration / maxIterations) * 100)
  }

  if (loading) {
    return (
      <div className="bg-gh-canvas-subtle p-6 rounded-md border border-gh-border">
        <div className="flex items-center justify-center">
          <Loader className="w-6 h-6 animate-spin text-gh-fg-muted mr-2" />
          <span className="text-gh-fg-muted">Loading repair cycle containers...</span>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="bg-red-100 dark:bg-red-900/20 p-4 rounded-md border border-red-300 dark:border-red-800">
        <div className="flex items-start">
          <AlertTriangle className="w-5 h-5 text-red-600 dark:text-red-400 mr-2 flex-shrink-0" />
          <div>
            <h3 className="text-sm font-semibold text-red-800 dark:text-red-300">Error</h3>
            <p className="text-sm text-red-700 dark:text-red-400 mt-1">{error}</p>
          </div>
        </div>
      </div>
    )
  }

  if (containers.length === 0) {
    return (
      <div className="bg-gh-canvas-subtle p-6 rounded-md border border-gh-border">
        <div className="text-center">
          <Wrench className="w-12 h-12 text-gh-fg-muted mx-auto mb-3" />
          <h3 className="text-lg font-semibold text-gh-fg-default mb-2">No Repair Cycles Running</h3>
          <p className="text-sm text-gh-fg-muted">
            Repair cycle containers will appear here when issues are moved to the Testing column.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-semibold text-gh-fg-default flex items-center">
          <Wrench className="w-5 h-5 mr-2" />
          Repair Cycle Containers
          <span className="ml-2 text-sm font-normal text-gh-fg-muted">
            ({containers.length} running)
          </span>
        </h2>
      </div>

      {containers.map((container) => {
        const key = `${container.project}-${container.issue_number}`
        const isExpanded = expandedContainer === container.container_name
        const statusColor = getStatusColor(container)
        const progress = getProgress(container)
        
        return (
          <div 
            key={container.container_name}
            className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden hover:border-gh-border-muted transition-colors"
          >
            {/* Header */}
            <div 
              className="p-4 cursor-pointer"
              onClick={() => toggleExpanded(container.container_name)}
            >
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  {/* Project and Issue */}
                  <div className="flex items-center gap-3 mb-2">
                    {getStatusIcon(container)}
                    <h3 className="text-lg font-semibold text-gh-fg-default">
                      {container.project}
                    </h3>
                    <span className="text-sm bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 px-2 py-0.5 rounded">
                      #{container.issue_number}
                    </span>
                    <span className="text-xs text-gh-fg-muted font-mono">
                      {container.run_id}
                    </span>
                  </div>

                  {/* Progress bar */}
                  {!container.is_finished && container.checkpoint && (
                    <div className="mb-3">
                      <div className="flex items-center justify-between text-xs text-gh-fg-muted mb-1">
                        <span>
                          {container.checkpoint.test_type} - Iteration {container.checkpoint.iteration}
                        </span>
                        <span>{progress.toFixed(0)}%</span>
                      </div>
                      <div className="h-2 bg-gh-canvas rounded-full overflow-hidden">
                        <div 
                          className={`h-full ${statusColor} transition-all duration-500`}
                          style={{ width: `${progress}%` }}
                        />
                      </div>
                    </div>
                  )}

                  {/* Status badges */}
                  <div className="flex flex-wrap gap-2 text-xs">
                    {container.checkpoint && (
                      <>
                        <span className="bg-gh-canvas px-2 py-1 rounded border border-gh-border">
                          <Wrench className="inline w-3 h-3 mr-1" />
                          {container.checkpoint.agent_call_count} agent calls
                        </span>
                        <span className="bg-gh-canvas px-2 py-1 rounded border border-gh-border">
                          <FileText className="inline w-3 h-3 mr-1" />
                          {container.checkpoint.files_fixed?.length || 0} files fixed
                        </span>
                      </>
                    )}
                    <span className="bg-gh-canvas px-2 py-1 rounded border border-gh-border">
                      Running: {formatDuration(container.created_at)}
                    </span>
                    {container.checkpoint && (
                      <span className="bg-gh-canvas px-2 py-1 rounded border border-gh-border">
                        Checkpoint: {Math.floor((container.checkpoint.checkpoint_age_seconds || 0))}s ago
                      </span>
                    )}
                  </div>
                </div>

                {/* Actions */}
                <div className="ml-4 flex gap-2">
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      killContainer(container.project, container.issue_number)
                    }}
                    disabled={killingContainer[key]}
                    className="px-3 py-2 bg-red-600 hover:bg-red-700 disabled:bg-red-400 text-white rounded-md text-sm transition-colors flex items-center gap-2"
                  >
                    {killingContainer[key] ? (
                      <>
                        <Loader className="w-4 h-4 animate-spin" />
                        Killing...
                      </>
                    ) : (
                      <>
                        <Trash2 className="w-4 h-4" />
                        Kill
                      </>
                    )}
                  </button>
                </div>
              </div>
            </div>

            {/* Expanded section - Logs */}
            {isExpanded && (
              <div className="border-t border-gh-border bg-gh-canvas p-4">
                <h4 className="text-sm font-semibold text-gh-fg-default mb-2">Container Logs</h4>
                {loadingLogs[key] ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader className="w-5 h-5 animate-spin text-gh-fg-muted mr-2" />
                    <span className="text-sm text-gh-fg-muted">Loading logs...</span>
                  </div>
                ) : containerLogs[key] ? (
                  <pre className="text-xs bg-black text-green-400 p-4 rounded-md overflow-x-auto max-h-96 overflow-y-auto font-mono">
                    {containerLogs[key]}
                  </pre>
                ) : (
                  <div className="text-sm text-gh-fg-muted text-center py-4">
                    Click to load logs
                  </div>
                )}
                
                <div className="mt-3 flex gap-2">
                  <button
                    onClick={() => fetchLogs(container.project, container.issue_number)}
                    className="px-3 py-1 bg-gh-canvas-subtle hover:bg-gh-border-muted border border-gh-border rounded text-sm"
                  >
                    Refresh Logs
                  </button>
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default RepairCycleContainers
