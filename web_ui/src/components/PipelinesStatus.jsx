import { useState } from 'react'
import { projectApi } from '../services/projectApi'
import { useProjectState } from '../contexts/ProjectStateContext'

export default function PipelinesStatus({ pipelines, projectName }) {
  const [releasing, setReleasing] = useState({})
  const { refreshProjects } = useProjectState()

  const handleReleaseLock = async (pipelineBoard, issueNumber) => {
    const key = `${pipelineBoard}-${issueNumber}`
    setReleasing(prev => ({ ...prev, [key]: true }))

    try {
      const response = await projectApi.releasePipelineLock(projectName, pipelineBoard, issueNumber)
      if (response.success) {
        // Refresh project data to update lock status
        await refreshProjects()
      } else {
        alert(`Failed to release lock: ${response.message || 'Unknown error'}`)
      }
    } catch (error) {
      console.error('Error releasing lock:', error)
      alert(`Error releasing lock: ${error.message}`)
    } finally {
      setReleasing(prev => ({ ...prev, [key]: false }))
    }
  }

  return (
    <div className="bg-gh-canvas border border-gh-border rounded-md p-3">
      <span className="text-xs font-medium text-gh-fg-muted block mb-2">Pipelines</span>
      {pipelines && pipelines.length > 0 ? (
        <div className="flex flex-col gap-2">
          {pipelines.map((pipeline) => {
            const pipelineName = typeof pipeline === 'string' ? pipeline : pipeline.name
            const pipelineBoard = typeof pipeline === 'object' ? pipeline.board : pipelineName
            const lock = typeof pipeline === 'object' ? pipeline.lock : null
            const releaseKey = lock ? `${pipelineBoard}-${lock.locked_by_issue}` : null

            return (
              <div key={pipelineName} className="flex items-center justify-between gap-2">
                <span className="inline-block px-2 py-1 rounded-md text-xs bg-gh-accent-emphasis/10 text-gh-accent-fg border border-gh-accent-emphasis/20">
                  {pipelineName}
                </span>
                {lock && lock.is_locked && (
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-yellow-500">
                      🔒 Issue #{lock.locked_by_issue}
                    </span>
                    <button
                      onClick={() => handleReleaseLock(pipelineBoard, lock.locked_by_issue)}
                      disabled={releasing[releaseKey]}
                      className="px-2 py-1 text-xs bg-red-500/10 text-red-500 border border-red-500/20 rounded hover:bg-red-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
                      title={`Release lock held by issue #${lock.locked_by_issue}`}
                    >
                      {releasing[releaseKey] ? 'Releasing...' : 'Release'}
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <p className="text-xs text-gh-fg-muted">No pipelines configured</p>
      )}
    </div>
  )
}
