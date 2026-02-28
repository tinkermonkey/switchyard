import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  ReactFlow,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { RefreshCw } from 'lucide-react'
import CycleBoundingNode from './CycleBoundingNode'
import PipelineEventNode from './PipelineEventNode'
import ReviewCycleContainerNode from './ReviewCycleContainerNode'
import RepairCycleContainerNode from './RepairCycleContainerNode'
import IterationContainerNode from './IterationContainerNode'
import LayoutController from './LayoutController'
import SmartPipelineEdge from './SmartPipelineEdge'

/**
 * Canonical layout options — the single source of truth for graph layout parameters.
 *
 * These values are tuned in the dev:sandbox environment (npm run dev:sandbox). When
 * you find better values there, update them here. All consumers (pipeline-run route,
 * layout-sandbox route, standalone sandbox) will automatically inherit the change:
 *   - Routes use these as their default layout options
 *   - Sandbox slider controls import and initialise from these defaults
 *
 * Exported so sandboxes can use them as initial slider values.
 */
export const DEFAULT_LAYOUT_OPTIONS = {
  nodeWidth: 250,
  nodeHeight: 80,
  horizontalSpacing: 150,
  verticalSpacing: 120,
  cycleGap: 100,
  cyclePadding: 40,
  viewportWidth: 1200,
}

const nodeTypes = {
  pipelineEvent: PipelineEventNode,
  cycleBounding: CycleBoundingNode,
  reviewCycleContainer: ReviewCycleContainerNode,
  repairCycleContainer: RepairCycleContainerNode,
  iterationContainer: IterationContainerNode,
  subCycleContainer: IterationContainerNode,  // same component, styled via cycleType data prop
}

const edgeTypes = {
  smart: SmartPipelineEdge,
}

const CYCLE_CONTAINER_TYPES = ['cycleBounding', 'reviewCycleContainer', 'repairCycleContainer']

/**
 * Shared pipeline flow graph component used by both the /pipeline-run view and the
 * layout sandbox. Manages all ReactFlow state (nodes, edges, hover) internally and
 * exposes a clean props API for the caller.
 *
 * Layout options are merged with DEFAULT_LAYOUT_OPTIONS so callers only need to pass
 * overrides — all params are always well-defined and reflect the sandbox-tuned values.
 *
 * Props:
 *   rawBuild        - Output of buildFlowchart() — unpositioned nodes + edges
 *   onToggleCycle   - Callback(cycleId) to toggle cycle collapse in parent state
 *   layoutOptions   - Partial layout options — merged with DEFAULT_LAYOUT_OPTIONS
 *   nodesDraggable  - Whether nodes can be dragged (default: true)
 *   allowResizing   - Whether cycleBounding nodes can be resized (default: true)
 *   minZoom         - React Flow minZoom (default: 0.3)
 *   maxZoom         - React Flow maxZoom (default: 2)
 *   height          - Container height as px number or CSS string (default: 600)
 *   loading         - Show loading spinner instead of graph (default: false)
 *   emptyMessage    - Message shown when there are no nodes to display
 *   onLayoutDone    - Optional callback(finalNodes) after layout completes
 *   fitViewAlign    - 'center' | 'top' | 'bottom' — initial viewport framing after layout.
 *                    'top' shows the first events; 'bottom' shows the most recent events;
 *                    both fit to graph width for zoom. Defaults to 'center'.
 */
export default function PipelineFlowGraph({
  rawBuild,
  onToggleCycle,
  layoutOptions,
  nodesDraggable = true,
  allowResizing = true,
  minZoom = 0.3,
  maxZoom = 2,
  height = 600,
  loading = false,
  emptyMessage = 'No events found',
  onLayoutDone,
  fitViewAlign = 'center',
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [hoveredNode, setHoveredNode] = useState(null)

  // Merge caller overrides with canonical defaults so every param is always defined.
  // Memoized so the reference is stable — prevents LayoutController's param-change
  // effect from firing on every render when layout options haven't actually changed.
  const mergedLayoutOptions = useMemo(
    () => ({ ...DEFAULT_LAYOUT_OPTIONS, ...layoutOptions }),
    [layoutOptions]
  )

  // Phase 1: set raw unpositioned nodes for React Flow to render and measure
  useEffect(() => {
    if (!rawBuild || rawBuild.nodes.length === 0) {
      setNodes([])
      setEdges([])
      return
    }
    setNodes(rawBuild.nodes)
  }, [rawBuild, setNodes, setEdges])

  // Inject draggable/toggle callbacks after layout — passed to LayoutController
  const finalizeNodes = useCallback((layoutedNodes) => {
    return layoutedNodes.map(node => {
      if (CYCLE_CONTAINER_TYPES.includes(node.type)) {
        return {
          ...node,
          ...(nodesDraggable && { draggable: true }),
          data: {
            ...node.data,
            onToggleCollapse: onToggleCycle,
            isResizable: allowResizing && node.type === 'cycleBounding',
          },
        }
      }
      return nodesDraggable ? { ...node, draggable: true } : node
    })
  }, [nodesDraggable, allowResizing, onToggleCycle])

  const onNodeMouseEnter = useCallback((event, node) => setHoveredNode(node), [])
  const onNodeMouseLeave = useCallback(() => setHoveredNode(null), [])

  const containerStyle = {
    height: typeof height === 'number' ? `${height}px` : height,
    position: 'relative',
  }

  if (loading) {
    return (
      <div style={containerStyle} className="flex items-center justify-center">
        <RefreshCw className="w-8 h-8 animate-spin text-gh-accent-primary" />
      </div>
    )
  }

  return (
    <div style={containerStyle}>
      {nodes.length === 0 ? (
        <div className="flex items-center justify-center h-full">
          <p className="text-gh-fg-muted">{emptyMessage}</p>
        </div>
      ) : (
        <>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeMouseEnter={onNodeMouseEnter}
            onNodeMouseLeave={onNodeMouseLeave}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            nodesDraggable={nodesDraggable}
            nodesConnectable={false}
            fitView
            fitViewOptions={{ padding: 0.05 }}
            minZoom={minZoom}
            maxZoom={maxZoom}
            zoomOnScroll={false}
            panOnScroll={true}
          >
            <LayoutController
              rawBuild={rawBuild}
              layoutOptions={mergedLayoutOptions}
              finalizeNodes={finalizeNodes}
              setNodes={setNodes}
              setEdges={setEdges}
              onLayoutDone={onLayoutDone}
              containerHeight={height}
              fitViewAlign={fitViewAlign}
            />
            <Background />
            <Controls />
          </ReactFlow>

          {hoveredNode && hoveredNode.data.metadata && (
            <div className="absolute top-4 right-4 bg-gh-canvas-inset border border-gh-border rounded-md p-3 shadow-lg max-w-sm z-10">
              <div className="font-semibold text-sm mb-1">{hoveredNode.data.label}</div>
              <div className="text-xs text-gh-fg-muted">{hoveredNode.data.metadata}</div>
            </div>
          )}
        </>
      )}

      <style>{`
        @keyframes stripes {
          0%   { background-position: 0 0; }
          100% { background-position: 1rem 1rem; }
        }
      `}</style>
    </div>
  )
}
