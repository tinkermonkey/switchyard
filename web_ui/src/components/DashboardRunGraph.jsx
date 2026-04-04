import { useCallback } from 'react'
import { useNavigate } from '@tanstack/react-router'
import PipelineFlowGraph from './PipelineFlowGraph'
import RunDuration from './RunDuration'
import CopyableId from './CopyableId'
import { useDashboardRunData } from '../hooks/useDashboardRunData'

export default function DashboardRunGraph({ run }) {
  const navigate = useNavigate()
  const { graphEvents, mergedEvents, workflowConfig, loading } = useDashboardRunData(run)

  const handleClick = useCallback(() => {
    navigate({ to: '/pipeline-run', search: { runId: run.id } })
  }, [navigate, run.id])

  return (
    <div
      className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden hover:border-gh-accent-primary transition-colors flex flex-col"
    >
      {/* Compact header */}
      <div className="flex items-center gap-3 px-3 py-2 border-b border-gh-border min-w-0 flex-shrink-0">
        <h3
          onClick={handleClick}
          className="text-sm font-semibold truncate min-w-0 flex-1 cursor-pointer">
          {run.issue_title}
        </h3>
        {run.issue_url ? (
          <a
            href={run.issue_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-gh-accent-fg hover:underline flex-shrink-0"
            onClick={e => e.stopPropagation()}
          >
            #{run.issue_number}
          </a>
        ) : (
          <span className="text-xs text-gh-fg-muted flex-shrink-0">#{run.issue_number}</span>
        )}
        <CopyableId id={run.id} className="text-xs text-gh-fg-muted flex-shrink-0" />
        <RunDuration
          startedAt={run.started_at}
          endedAt={run.ended_at}
          className="text-xs text-gh-fg-muted flex-shrink-0"
        />
      </div>

      {/* Graph body */}
      <div className="flex-1 min-h-0">
        <PipelineFlowGraph
          graphEvents={graphEvents}
          allEvents={mergedEvents}
          workflowConfig={workflowConfig}
          selectedPipelineRun={run}
          height="100%"
          fitViewAlign="center"
          showAllNodes={false}
          minZoom={0.1}
          maxZoom={0.8}
          loading={loading}
          emptyMessage="Waiting for events..."
        />
      </div>
    </div>
  )
}
