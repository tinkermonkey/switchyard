import React, { useState } from 'react'
import { AlertTriangle, XCircle, Activity } from 'lucide-react'
import { useActiveAgents } from '../hooks/useActiveAgents'
import { useAgentActions } from '../hooks/useAgentActions'
import { formatDuration, getRuntimeMs } from '../utils/stateHelpers'

/**
 * Format agent name to human-readable format
 * e.g., "product_manager_agent" -> "Product Manager Agent"
 */
const formatAgentName = (agentName) => {
  if (!agentName) return 'Unknown Agent'
  return agentName.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
}

/**
 * Get status color based on agent runtime
 * Green: < 5 minutes (normal)
 * Yellow: 5-15 minutes (warning)
 * Red: > 15 minutes (alert)
 */
const getAgentStatusColor = (startedAt) => {
  const runtimeMs = getRuntimeMs(startedAt)
  const runtimeMinutes = runtimeMs / (1000 * 60)
  
  if (runtimeMinutes < 5) {
    return 'bg-green-500'
  } else if (runtimeMinutes < 15) {
    return 'bg-yellow-500'
  } else {
    return 'bg-red-500'
  }
}

/**
 * ActiveAgents - Display active agents with optional container component
 * @param {React.Component} ContainerComponent - Component to wrap each agent (default: div)
 * @param {string} containerClassName - Additional classes for the container
 */
const ActiveAgents = ({
  ContainerComponent = 'div',
  containerClassName = 'bg-gh-canvas p-3 rounded-md border border-gh-border'
}) => {
  const { agents: activeAgents, agentCount, loading, fetchError } = useActiveAgents()
  const { killAgent, isKillingAgent, error: actionError, clearError } = useAgentActions()
  const [showKillModal, setShowKillModal] = useState(false)
  const [selectedAgent, setSelectedAgent] = useState(null)

  const handleKillClick = (agent) => {
    setSelectedAgent(agent)
    setShowKillModal(true)
    clearError()
  }

  const confirmKillAgent = async () => {
    if (!selectedAgent) return

    // Only containerized agents can be killed via container name
    if (!selectedAgent.container_name) {
      setShowKillModal(false)
      setSelectedAgent(null)
      return
    }

    const containerName = selectedAgent.container_name
    setShowKillModal(false)

    try {
      await killAgent(containerName)
      // Agent will be removed from list automatically when events update
    } catch (err) {
      // Error is handled by the hook and displayed in the UI
      console.error('Error killing agent:', err)
    } finally {
      setSelectedAgent(null)
    }
  }

  // Render a single agent card
  const renderAgentCard = (agent, idx) => {
    const readableName = formatAgentName(agent.agent)
    const containerProps = typeof ContainerComponent === 'string' 
      ? { className: containerClassName }
      : { title: readableName, minWidth: 'min-w-[200px]' }
    
    const statusColor = getAgentStatusColor(agent.started_at)

    return (
      <ContainerComponent key={`${agent.agent}-${agent.task_id}-${idx}`} {...containerProps}>
        <div className="space-y-2">
          {/* Status indicator with candy stripe animation */}
          <div className="relative h-1 bg-gh-canvas-subtle rounded-full overflow-hidden">
            <div 
              className={`absolute inset-0 ${statusColor} animate-pulse`}
              style={{
                backgroundImage: 'linear-gradient(45deg, rgba(255,255,255,.2) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.2) 50%, rgba(255,255,255,.2) 75%, transparent 75%, transparent)',
                backgroundSize: '1rem 1rem',
                animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite, stripes 1s linear infinite'
              }}
            />
          </div>

          {/* Agent badges */}
          <div className="flex items-center gap-2 flex-wrap">
            {agent.project && (
              <span className="text-xs bg-gh-canvas-subtle text-gh-fg-muted px-2 py-0.5 rounded border border-gh-border">
                {agent.project}
              </span>
            )}
            {agent.branch_name && (
              <span className="text-xs bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 px-2 py-0.5 rounded border border-purple-200 dark:border-purple-800">
                {agent.branch_name}
              </span>
            )}
            {agent.issue_number && agent.issue_number !== 'unknown' && (
              <span className="text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 px-2 py-0.5 rounded">
                #{agent.issue_number}
              </span>
            )}
            {!agent.is_containerized && (
              <span className="text-xs bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 px-2 py-0.5 rounded">
                native
              </span>
            )}
          </div>

          {/* Agent details */}
          <div className="text-xs text-gh-fg-muted space-y-1">
            <div className="flex items-center justify-between">
              <span>Running:</span>
              <span className="font-medium text-gh-fg-default">{formatDuration(agent.started_at)}</span>
            </div>
            
            {agent.container_name && (
              <div className="flex items-center justify-between">
                <span>Container:</span>
                <span className="font-mono text-[10px] text-gh-fg-muted truncate max-w-[120px]" title={agent.container_name}>
                  {agent.container_name.split('-').pop()}
                </span>
              </div>
            )}
          </div>

          {/* Action button */}
          {agent.is_containerized && (
            <button
              onClick={() => handleKillClick(agent)}
              disabled={isKillingAgent(agent.container_name)}
              className={`
                px-1 py-1 rounded text-xs font-SM flex items-center justify-center gap-1.5
                ${isKillingAgent(agent.container_name)
                  ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
                  : 'bg-red-600 text-white hover:bg-red-700 active:bg-red-800'
                }
                transition-colors
              `}
              title="Emergency kill switch - immediately stop this agent"
            >
              <XCircle className="w-3 h-3" />
              {isKillingAgent(agent.container_name) ? 'Killing...' : 'Kill'}
            </button>
          )}
        </div>
      </ContainerComponent>
    )
  }

  return (
    <>
      {actionError && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded flex items-center gap-2 text-red-700 dark:text-red-400">
          <AlertTriangle className="w-4 h-4" />
          {actionError}
        </div>
      )}

      {fetchError && (
        <div className="mb-4 p-3 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded flex items-center gap-2 text-yellow-700 dark:text-yellow-400">
          <AlertTriangle className="w-4 h-4" />
          Error fetching agents: {fetchError}
        </div>
      )}

      {activeAgents.length === 0 ? (
        typeof ContainerComponent === 'string' ? (
          <div className={containerClassName}>
            <div className="text-center text-gh-fg-muted text-sm py-2">
              <Activity className="w-6 h-6 mx-auto mb-1 opacity-50" />
              <p>No active agents</p>
            </div>
          </div>
        ) : (
          <ContainerComponent title="Active Agents" minWidth="min-w-[180px]">
            <div className="text-center text-gh-fg-muted text-sm py-2">
              <Activity className="w-6 h-6 mx-auto mb-1 opacity-50" />
              <p>No active agents</p>
            </div>
          </ContainerComponent>
        )
      ) : (
        activeAgents.map((agent, idx) => renderAgentCard(agent, idx))
      )}

      {/* Custom Modal */}
      {showKillModal && (
        <div className="fixed inset-0 z-50 overflow-y-auto">
          {/* Backdrop */}
          <div
            className="fixed inset-0 bg-black bg-opacity-50 transition-opacity"
            onClick={() => setShowKillModal(false)}
          />

          {/* Modal */}
          <div className="flex min-h-full items-center justify-center p-4">
            <div className="relative bg-white rounded-lg shadow-xl max-w-md w-full">
              {/* Header */}
              <div className="flex items-center justify-between p-4 border-b">
                <h3 className="text-xl font-semibold text-gray-900">
                  Confirm Kill Agent
                </h3>
                <button
                  onClick={() => setShowKillModal(false)}
                  className="text-gray-400 hover:text-gray-600"
                >
                  <XCircle className="w-5 h-5" />
                </button>
              </div>

              {/* Body */}
              <div className="p-6 space-y-4">
                <p className="text-base leading-relaxed text-gray-700">
                  Are you sure you want to <strong className="text-red-600">KILL</strong> this agent container?
                </p>
                {selectedAgent && (
                  <div className="bg-gray-50 p-3 rounded border border-gray-200">
                    <p className="text-sm font-medium text-gray-900">{selectedAgent.agent}</p>
                    <p className="text-xs text-gray-600 mt-1">Project: {selectedAgent.project}</p>
                    {selectedAgent.container_name && (
                      <p className="text-xs text-gray-600">Container: {selectedAgent.container_name}</p>
                    )}
                  </div>
                )}
                <p className="text-sm text-gray-600">
                  This will immediately stop the running agent and terminate all ongoing work.
                </p>
              </div>

              {/* Footer */}
              <div className="flex items-center gap-3 p-4 border-t justify-end">
                <button
                  onClick={() => setShowKillModal(false)}
                  className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={confirmKillAgent}
                  className="px-4 py-2 text-sm font-medium text-white bg-red-600 rounded-lg hover:bg-red-700 transition-colors"
                >
                  Kill Agent
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export default ActiveAgents
