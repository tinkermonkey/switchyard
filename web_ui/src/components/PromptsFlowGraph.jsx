import { useState, useMemo, useEffect, useRef, useCallback } from 'react'
import { ReactFlow, Background, Controls, useNodesState, useEdgesState } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { eventNodeTypes } from './nodes/index.js'
import PromptInputNode from './nodes/prompts/PromptInputNode.jsx'
import PromptOutputNode from './nodes/prompts/PromptOutputNode.jsx'
import { buildPromptsGraph } from '../utils/buildPromptsGraph.js'

const nodeTypes = {
  pipelineStarted: eventNodeTypes.pipelineStarted,
  agentExecution:  eventNodeTypes.agentExecution,
  promptInput:     PromptInputNode,
  promptOutput:    PromptOutputNode,
}

export default function PromptsFlowGraph({ events, selectedPipelineRun, loading }) {
  const [filterAgentName, setFilterAgentName] = useState('')
  const [showInput, setShowInput]   = useState(true)
  const [showOutput, setShowOutput] = useState(true)

  const rfRef = useRef(null)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])

  // Distinct agent names for the filter dropdown
  const agentNames = useMemo(() => {
    if (!events?.length) return []
    const names = new Set()
    for (const e of events) {
      if (e.event_type === 'agent_initialized' && e.agent) names.add(e.agent)
    }
    return [...names].sort()
  }, [events])

  const { nodes: rawNodes, edges: rawEdges } = useMemo(() =>
    buildPromptsGraph({
      events: events ?? [],
      selectedPipelineRun,
      filters: {
        agentName: filterAgentName || null,
        showInput,
        showOutput,
      },
    }),
    [events, selectedPipelineRun, filterAgentName, showInput, showOutput]
  )

  useEffect(() => {
    setNodes(rawNodes)
    setEdges(rawEdges)
    const timer = setTimeout(() => {
      rfRef.current?.fitView({ padding: 0.05 })
    }, 50)
    return () => clearTimeout(timer)
  }, [rawNodes, rawEdges, setNodes, setEdges])

  const onInit = useCallback(inst => { rfRef.current = inst }, [])

  const isEmpty = !loading && rawNodes.length === 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <style>{`
        @keyframes stripes {
          0%   { background-position: 0 0; }
          100% { background-position: 1rem 1rem; }
        }
      `}</style>

      {/* Filter bar */}
      <div className="flex items-center gap-4 px-3 py-2 border-b border-gh-border flex-shrink-0 bg-gh-canvas-subtle">
        <span className="text-xs text-gh-fg-muted">Agent</span>
        <select
          value={filterAgentName}
          onChange={e => setFilterAgentName(e.target.value)}
          className="text-xs bg-gh-canvas border border-gh-border rounded px-2 py-1 text-gh-fg"
        >
          <option value="">All agents</option>
          {agentNames.map(name => (
            <option key={name} value={name}>{name.replace(/_/g, ' ')}</option>
          ))}
        </select>

        <label className="flex items-center gap-1.5 text-xs text-gh-fg-muted cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showInput}
            onChange={e => setShowInput(e.target.checked)}
            className="cursor-pointer"
          />
          Show Input
        </label>

        <label className="flex items-center gap-1.5 text-xs text-gh-fg-muted cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showOutput}
            onChange={e => setShowOutput(e.target.checked)}
            className="cursor-pointer"
          />
          Show Output
        </label>
      </div>

      {/* Graph area */}
      <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center text-gh-fg-muted text-sm z-10">
            Loading events…
          </div>
        )}
        {isEmpty && !loading && (
          <div className="absolute inset-0 flex items-center justify-center text-gh-fg-muted text-sm z-10">
            No agent prompt events found for this pipeline run
          </div>
        )}
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          onInit={onInit}
          nodesDraggable={false}
          nodesConnectable={false}
          minZoom={0.05}
          maxZoom={1.5}
          zoomOnScroll={false}
          panOnScroll={true}
        >
          <Background />
          <Controls />
        </ReactFlow>
      </div>
    </div>
  )
}
