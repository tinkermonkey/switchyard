import React from 'react'
import { Link } from '@tanstack/react-router'
import { Wrench, Loader, AlertTriangle, CheckCircle, XCircle, ArrowRight } from 'lucide-react'
import { useRepairCycles } from '../hooks/useRepairCycles'
import { formatDuration } from '../utils/stateHelpers'

/**
 * RepairCycleCard - Compact card showing repair cycle status for dashboard
 */
const RepairCycleCard = () => {
  const { containers, containerCount, loading, error } = useRepairCycles()

  // Calculate stats
  const stats = {
    running: containers.filter(c => !c.is_finished).length,
    completed: containers.filter(c => c.is_finished && c.result?.overall_success).length,
    failed: containers.filter(c => c.is_finished && !c.result?.overall_success).length,
    stale: containers.filter(c => {
      if (c.is_finished) return false
      const checkpoint = c.checkpoint
      if (!checkpoint) return c.container_age_seconds > 600 // 10 min without checkpoint
      return checkpoint.checkpoint_age_seconds > 600 // 10 min stale checkpoint
    }).length
  }

  // Get status color for container
  const getStatusColor = (container) => {
    if (container.is_finished) {
      return container.result?.overall_success ? 'border-green-500' : 'border-red-500'
    }
    
    const checkpoint = container.checkpoint
    if (!checkpoint) {
      if (container.container_age_seconds < 300) return 'border-blue-500'
      return 'border-yellow-500'
    }
    
    const checkpointAge = checkpoint.checkpoint_age_seconds || 0
    if (checkpointAge < 300) return 'border-green-500'
    if (checkpointAge < 600) return 'border-yellow-500'
    return 'border-red-500'
  }

  // Get status icon
  const getStatusIcon = (container) => {
    if (container.is_finished) {
      return container.result?.overall_success ? 
        <CheckCircle className="w-4 h-4 text-green-500" /> : 
        <XCircle className="w-4 h-4 text-red-500" />
    }
    
    const checkpoint = container.checkpoint
    if (!checkpoint || (checkpoint.checkpoint_age_seconds || 0) > 600) {
      return <AlertTriangle className="w-4 h-4 text-yellow-500" />
    }
    
    return <Loader className="w-4 h-4 text-blue-500 animate-spin" />
  }

  if (loading) {
    return (
      <div className="bg-gh-canvas-subtle p-6 rounded-md border border-gh-border">
        <div className="flex items-center">
          <Loader className="w-5 h-5 animate-spin text-gh-fg-muted mr-2" />
          <span className="text-sm text-gh-fg-muted">Loading repair cycles...</span>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="bg-red-100 dark:bg-red-900/20 p-4 rounded-md border border-red-300 dark:border-red-800">
        <div className="flex items-start">
          <AlertTriangle className="w-5 h-5 text-red-600 dark:text-red-400 mr-2" />
          <div>
            <h3 className="text-sm font-semibold text-red-800 dark:text-red-300">Error</h3>
            <p className="text-xs text-red-700 dark:text-red-400 mt-1">{error}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-gh-canvas-subtle p-6 rounded-md border border-gh-border">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center">
          <Wrench className="w-5 h-5 text-gh-fg-default mr-2" />
          <h3 className="text-lg font-semibold text-gh-fg-default">Repair Cycles</h3>
        </div>
        <Link 
          to="/repair-cycles"
          className="text-sm text-gh-accent-fg hover:underline flex items-center"
        >
          View All <ArrowRight className="w-4 h-4 ml-1" />
        </Link>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-3 mb-4">
        <div className="bg-gh-canvas p-3 rounded border border-gh-border text-center">
          <div className="text-2xl font-bold text-gh-fg-default">{stats.running}</div>
          <div className="text-xs text-gh-fg-muted">Running</div>
        </div>
        <div className="bg-gh-canvas p-3 rounded border border-green-500/30 text-center">
          <div className="text-2xl font-bold text-green-600 dark:text-green-400">{stats.completed}</div>
          <div className="text-xs text-gh-fg-muted">Completed</div>
        </div>
        <div className="bg-gh-canvas p-3 rounded border border-red-500/30 text-center">
          <div className="text-2xl font-bold text-red-600 dark:text-red-400">{stats.failed}</div>
          <div className="text-xs text-gh-fg-muted">Failed</div>
        </div>
        <div className="bg-gh-canvas p-3 rounded border border-yellow-500/30 text-center">
          <div className="text-2xl font-bold text-yellow-600 dark:text-yellow-400">{stats.stale}</div>
          <div className="text-xs text-gh-fg-muted">Stale</div>
        </div>
      </div>

      {/* Container list */}
      {containerCount === 0 ? (
        <div className="text-center py-6 text-gh-fg-muted">
          <p className="text-sm">No repair cycles running</p>
        </div>
      ) : (
        <div className="space-y-2 max-h-64 overflow-y-auto">
          {containers.slice(0, 5).map((container) => (
            <Link
              key={container.container_name}
              to="/repair-cycles"
              className={`block p-3 bg-gh-canvas rounded border-l-4 ${getStatusColor(container)} hover:bg-gh-border-muted transition-colors`}
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    {getStatusIcon(container)}
                    <span className="font-semibold text-sm text-gh-fg-default truncate">
                      {container.project}
                    </span>
                    <span className="text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 px-2 py-0.5 rounded">
                      #{container.issue_number}
                    </span>
                  </div>
                  
                  {container.checkpoint && (
                    <div className="text-xs text-gh-fg-muted">
                      {container.checkpoint.test_type} - Iteration {container.checkpoint.iteration} - {container.checkpoint.agent_call_count} calls
                    </div>
                  )}
                  
                  <div className="text-xs text-gh-fg-muted mt-1">
                    Running: {formatDuration(container.created_at)}
                    {container.checkpoint && (
                      <> • Checkpoint: {Math.floor(container.checkpoint.checkpoint_age_seconds || 0)}s ago</>
                    )}
                  </div>
                </div>
              </div>
            </Link>
          ))}
          
          {containerCount > 5 && (
            <div className="text-center pt-2">
              <Link 
                to="/repair-cycles"
                className="text-sm text-gh-accent-fg hover:underline"
              >
                + {containerCount - 5} more
              </Link>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default RepairCycleCard
