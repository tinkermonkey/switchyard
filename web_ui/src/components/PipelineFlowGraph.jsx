import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  ReactFlow,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { RefreshCw } from 'lucide-react'
import LayoutController from './LayoutController'
import SmartPipelineEdge from './SmartPipelineEdge'
import { containerNodeTypes } from './containers/index.js'
import { eventNodeTypes } from './nodes/index.js'
import { toggleCycleCollapsed } from '../utils/cycleLayout'
import { buildFlowchart as buildFlowchartUtil } from '../utils/buildFlowchart'
import { processEvents } from '../utils/eventProcessing/index.js'

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
  horizontalSpacing: 100,
  verticalSpacing: 60,
  cycleGap: 60,
  cyclePadding: 40,
  viewportWidth: 1200,
  iterHeaderHeight: 30,
  iterPadding: 20,
  innerVertSpacing: 60,
  innerHorizSpacing: 60,
  containerHeaderHeight: 36,
}

const nodeTypes = {
  // Container / structural nodes (not event-type-specific).
  // Sourced from containers/index.js.
  ...containerNodeTypes,

  // Event nodes — one component per event type, with 'pipelineEvent' as fallback.
  // Sourced from nodes/index.js; mapping defined in nodes/EVENT_TYPE_MAP.js.
  ...eventNodeTypes,
}

const edgeTypes = {
  smart: SmartPipelineEdge,
}

const CYCLE_CONTAINER_TYPES = Object.keys(containerNodeTypes)

/**
 * Shared pipeline flow graph component used by both the /pipeline-run view and the
 * layout sandbox. Manages all ReactFlow state (nodes, edges, hover) internally and
 * exposes a clean props API for the caller.
 *
 * Layout options are merged with DEFAULT_LAYOUT_OPTIONS so callers only need to pass
 * overrides — all params are always well-defined and reflect the sandbox-tuned values.
 *
 * Props:
 *   graphEvents         - Filtered events array (no claude_log events)
 *   allEvents           - Full unfiltered event array (incl. claude_log) for token/tool enrichment (optional)
 *   workflowConfig      - Workflow config object
 *   selectedPipelineRun - Pipeline run object
 *   onModelChange       - Optional callback(model) fired after each buildFlowchart call
 *   layoutOptions       - Partial layout options — merged with DEFAULT_LAYOUT_OPTIONS
 *   nodesDraggable      - Whether nodes can be dragged (default: true)
 *   allowResizing       - Whether cycleBounding nodes can be resized (default: true)
 *   minZoom             - React Flow minZoom (default: 0.3)
 *   maxZoom             - React Flow maxZoom (default: 2)
 *   height              - Container height as px number or CSS string (default: 600)
 *   loading             - Show loading spinner instead of graph (default: false)
 *   emptyMessage        - Message shown when there are no nodes to display
 *   onLayoutDone        - Optional callback(finalNodes) after layout completes
 *   fitViewAlign        - 'center' | 'top' | 'bottom' | 'active-node' — initial viewport framing after layout.
 *                        'top' shows the first events; 'bottom' shows the most recent events;
 *                        'active-node' centres on the in-progress node (falls back to 'bottom');
 *                        both 'top'/'bottom' fit to graph width for zoom. Defaults to 'center'.
 *   showAllNodes        - Show all nodes including defaultHidden ones (default: true)
 */
export default function PipelineFlowGraph({
  graphEvents,
  allEvents = null,
  workflowConfig,
  selectedPipelineRun,
  onModelChange,
  layoutOptions,
  nodesDraggable = false,
  allowResizing = false,
  minZoom = 0.3,
  maxZoom = 2,
  height = 600,
  loading = false,
  emptyMessage = 'No events found',
  onLayoutDone,
  fitViewAlign = 'center',
  showAllNodes = true,
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [hoveredNode, setHoveredNode] = useState(null)
  const [layoutReady, setLayoutReady] = useState(false)
  // Increments only on structural changes; LayoutController uses this (not rawBuild)
  // to decide when to reset and re-run layout.
  const [structuralVersion, setStructuralVersion] = useState(0)
  // Increments on data-only changes (same node IDs, status/label updates); LayoutController
  // uses this to re-run layout with fresh measurements without resetting the viewport.
  const [dataVersion, setDataVersion] = useState(0)
  // Increments each time React Flow fires dimension events for rendered nodes.
  // LayoutController's Phase 2 depends on this so it can retry layout after nodes
  // are re-measured following a structural update (e.g. run switch with shared IDs).
  const [measurementVersion, setMeasurementVersion] = useState(0)
  // Tracks the node IDs from the last layout pass to distinguish structural
  // changes (new/removed nodes) from data-only updates (status/label changes).
  const prevNodeIdsRef = useRef(new Set())
  // True once the graph has completed its first layout — used to detect incremental
  // updates vs. a fresh initial draw.
  const hasLayoutedOnceRef = useRef(false)
  // Persistent cache of node measurements: nodeId → { width, height }.
  // Updated via onNodesChange dimension events so the layout function always has
  // accurate sizes even when node.measured hasn't been (re-)populated yet.
  const nodeSizeCacheRef = useRef(new Map())

  // Internal cycle collapse state — managed here so toggle callbacks work without
  // forwarding to a parent component.
  const [cycles, setCycles] = useState(new Map())
  const cyclesRef = useRef(new Map())
  useEffect(() => { cyclesRef.current = cycles }, [cycles])

  // Internally computed rawBuild (unpositioned nodes + edges from buildFlowchart).
  const [rawBuild, setRawBuild] = useState(null)
  // Throttle ref for rawBuild updates (≤ 2/sec during heavy socket activity)
  const rawBuildThrottleRef = useRef({ timer: null, lastRun: 0, pendingFn: null })

  // Stable ref for onModelChange so the callback doesn't cause effect re-runs.
  const onModelChangeRef = useRef(onModelChange)
  useEffect(() => { onModelChangeRef.current = onModelChange }, [onModelChange])

  // Merge caller overrides with canonical defaults so every param is always defined.
  // Memoized so the reference is stable — prevents LayoutController's param-change
  // effect from firing on every render when layout options haven't actually changed.
  const mergedLayoutOptions = useMemo(
    () => ({ ...DEFAULT_LAYOUT_OPTIONS, ...layoutOptions }),
    [layoutOptions]
  )

  const selectedRunId = selectedPipelineRun?.id

  // Reset cycle state when switching pipeline runs.
  useEffect(() => {
    setCycles(new Map())
    cyclesRef.current = new Map()
  }, [selectedRunId])

  // Build raw (unpositioned) flowchart — pure, returns result
  const buildRawFlowchart = useCallback(() => {
    if (!graphEvents || !graphEvents.length || !selectedPipelineRun) return null

    // Derive activeTaskIds from the zombie-aware agentExecutions map so that
    // interrupted (zombie) agents are excluded. The raw scan of agent_initialized
    // without agent_completed would incorrectly include zombies from orchestrator restarts.
    const { agentExecutions: execMap } = processEvents(graphEvents)
    const activeTaskIds = new Set()
    execMap.forEach(executions => {
      executions.forEach(exec => {
        if (exec.status === 'running') activeTaskIds.add(exec.taskId)
      })
    })

    return buildFlowchartUtil({
      events: graphEvents,
      allEvents,
      existingCycles: cyclesRef.current,  // ref is pre-updated before this fires (see below)
      workflowConfig,
      selectedPipelineRun,
      activeTaskIds,
    })
  // `cycles` in deps causes recreation on every toggle so the rebuild effect re-fires.
  // The function reads cyclesRef.current (not the closure value) because both the cycles
  // detection effect and handleToggleWithTracking pre-update the ref synchronously before
  // calling setCycles — guaranteeing the ref is current when this callback executes.
  }, [graphEvents, allEvents, selectedPipelineRun, workflowConfig, cycles])

  // Detect and update cycles when events change, preserving any user toggle state.
  useEffect(() => {
    if (!graphEvents || !graphEvents.length) return

    const model = processEvents(graphEvents)

    const newCycleMap = new Map()
    model.cycles.forEach(cycle => {
      // Use each cycle's own default (from processEvents) so types like
      // status_progression (isCollapsed: true) start collapsed out of the box.
      newCycleMap.set(cycle.id, { isCollapsed: cycle.isCollapsed ?? false })
    })

    const prevCycles = cyclesRef.current
    const merged = new Map(newCycleMap)
    prevCycles.forEach((prev, id) => {
      if (merged.has(id)) merged.get(id).isCollapsed = prev.isCollapsed
      else merged.set(id, prev)
    })

    cyclesRef.current = merged
    setCycles(merged)
  }, [graphEvents, selectedPipelineRun])

  // Rebuild flowchart when events or config change — throttled to ≤ 2/sec
  useEffect(() => {
    const throttle = rawBuildThrottleRef.current
    throttle.pendingFn = buildRawFlowchart

    const now = Date.now()
    const elapsed = now - throttle.lastRun

    if (elapsed >= 500) {
      throttle.lastRun = now
      const result = buildRawFlowchart() ?? null
      setRawBuild(result)
      if (result?.model) onModelChangeRef.current?.(result.model)
    } else if (!throttle.timer) {
      throttle.timer = setTimeout(() => {
        throttle.timer = null
        throttle.lastRun = Date.now()
        const result = throttle.pendingFn?.() ?? null
        setRawBuild(result)
        if (result?.model) onModelChangeRef.current?.(result.model)
      }, 500 - elapsed)
    }
  }, [buildRawFlowchart])

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      const { timer } = rawBuildThrottleRef.current
      if (timer) clearTimeout(timer)
    }
  }, [])

  // Remove nodes marked defaultHidden (boundary/housekeeping events) and reroute edges
  // around them so the sequential chain stays intact. Nodes are fully removed (not just
  // marked hidden) so the layout engine never allocates space for them. Skipped when
  // showAllNodes is true.
  const filteredRawBuild = useMemo(() => {
    if (!rawBuild || showAllNodes) return rawBuild

    const hiddenIds = new Set(
      rawBuild.nodes.filter(n => n.data?.defaultHidden).map(n => n.id)
    )
    if (hiddenIds.size === 0) return rawBuild

    const successors = new Map()
    rawBuild.edges.forEach(e => successors.set(e.source, e.target))

    function nextVisible(id) {
      let cur = successors.get(id)
      while (cur && hiddenIds.has(cur)) cur = successors.get(cur)
      return cur
    }

    const newEdges = []
    const seen = new Set()
    rawBuild.edges.forEach(e => {
      if (hiddenIds.has(e.source)) return
      const target = hiddenIds.has(e.target) ? nextVisible(e.target) : e.target
      if (!target) return
      const key = `${e.source}→${target}`
      if (seen.has(key)) return
      seen.add(key)
      newEdges.push(e.target === target ? e : { ...e, id: `edge-${e.source}-${target}`, target })
    })

    return {
      ...rawBuild,
      nodes: rawBuild.nodes.filter(node => !node.data?.defaultHidden),
      edges: newEdges,
    }
  }, [rawBuild, showAllNodes])

  // Phase 1: set raw unpositioned nodes for React Flow to render and measure.
  //
  // Structural changes (new or removed node IDs):
  //   - Incremental update (layout already done + some nodes are shared with previous set):
  //     Merge existing positions into incoming nodes so they don't jump to {0,0}. Graph stays
  //     visible throughout; LayoutController re-runs layout for new nodes only.
  //   - Initial draw or completely new graph (no node overlap with previous set):
  //     Hide the graph while ReactFlow remeasures and LayoutController repositions.
  //
  // Data-only updates (same node IDs, status/label changes): update node data
  // in-place preserving existing positions; graph stays visible, no layout needed.
  useEffect(() => {
    if (!filteredRawBuild || filteredRawBuild.nodes.length === 0) {
      prevNodeIdsRef.current = new Set()
      hasLayoutedOnceRef.current = false
      setNodes([])
      setEdges([])
      setLayoutReady(false)
      return
    }

    const newNodeIds = new Set(filteredRawBuild.nodes.map(n => n.id))
    const prevNodeIds = prevNodeIdsRef.current
    const structurallyChanged =
      prevNodeIds.size !== newNodeIds.size ||
      filteredRawBuild.nodes.some(n => !prevNodeIds.has(n.id))

    prevNodeIdsRef.current = newNodeIds

    if (structurallyChanged) {
      const anyOverlap = prevNodeIds.size > 0 && filteredRawBuild.nodes.some(n => prevNodeIds.has(n.id))
      const isIncrementalUpdate = hasLayoutedOnceRef.current && anyOverlap


      if (isIncrementalUpdate) {
        // Keep graph visible — carry existing positions and styles forward so container nodes
        // don't lose their computed dimensions (style.width/height) while LayoutController
        // re-runs. Without this, SmartPipelineEdge falls back to 200px obstacle widths.
        // Also preserve injected callbacks (onToggleCollapse) — raw build nodes have null
        // because finalizeNodes hasn't run yet; overwriting causes toggle controls to flicker.
        nodeSizeCacheRef.current = new Map()
        setNodes(prev => {
          const existingByNode = new Map(prev.map(n => [n.id, n]))
          return filteredRawBuild.nodes.map(n => {
            const existing = existingByNode.get(n.id)
            const data = existing?.data?.onToggleCollapse
              ? { ...n.data, onToggleCollapse: existing.data.onToggleCollapse }
              : n.data
            return {
              ...n,
              data,
              position: existing?.position ?? { x: 0, y: 0 },
              style: n.data?.isCollapsed ? n.style : (existing?.style ?? n.style),
            }
          })
        })
      } else {
        hasLayoutedOnceRef.current = false
        nodeSizeCacheRef.current = new Map()
        setLayoutReady(false)
        setNodes(filteredRawBuild.nodes)
      }
      setStructuralVersion(v => v + 1)
    } else {
      const newNodeById = new Map(filteredRawBuild.nodes.map(n => [n.id, n]))
      setNodes(prev => prev.map(node => {
        const newNode = newNodeById.get(node.id)
        if (!newNode) return node
        // Preserve injected callbacks — raw build nodes always have onToggleCollapse: null
        // (finalizeNodes hasn't re-run); overwriting causes toggle controls to flicker.
        const data = node.data?.onToggleCollapse
          ? { ...newNode.data, onToggleCollapse: node.data.onToggleCollapse }
          : newNode.data
        return { ...node, data, hidden: newNode.hidden ?? false }
      }))
      setDataVersion(v => v + 1)
    }
  }, [filteredRawBuild, setNodes, setEdges])

  // Stable toggle wrapper: tracks whether the user explicitly opened a container
  // so auto-collapse will not override it. Uses refs so this callback never changes
  // identity, keeping finalizeNodes (and therefore all container node onToggleCollapse
  // props) stable across re-renders.
  const handleToggleWithTracking = useCallback((cycleId) => {
    // Pre-update the ref synchronously so buildRawFlowchart reads the correct value
    // when the rebuild effect fires (same pattern as the cycles detection effect).
    const newCycles = toggleCycleCollapsed(cyclesRef.current, cycleId)
    cyclesRef.current = newCycles
    setCycles(newCycles)
  }, []) // stable — reads all mutable state via refs

  // Inject draggable/toggle callbacks after layout — passed to LayoutController
  const finalizeNodes = useCallback((layoutedNodes) => {
    return layoutedNodes.map(node => {
      if (CYCLE_CONTAINER_TYPES.includes(node.type)) {
        return {
          ...node,
          ...(nodesDraggable && { draggable: true }),
          data: {
            ...node.data,
            onToggleCollapse: handleToggleWithTracking,
            isResizable: allowResizing && node.type === 'cycleBounding',
          },
        }
      }
      return nodesDraggable ? { ...node, draggable: true } : node
    })
  }, [nodesDraggable, allowResizing, handleToggleWithTracking])

  const handleLayoutDone = useCallback((finalNodes) => {
    hasLayoutedOnceRef.current = true
    setLayoutReady(true)
    onLayoutDone?.(finalNodes)
  }, [onLayoutDone])

  const onNodeMouseEnter = useCallback((event, node) => setHoveredNode(node), [])
  const onNodeMouseLeave = useCallback(() => setHoveredNode(null), [])

  // Intercept React Flow's dimension-change events to populate the persistent size cache.
  const handleNodesChange = useCallback((changes) => {
    let gotDimensions = false
    changes.forEach(change => {
      if (change.type === 'dimensions' && change.dimensions) {
        nodeSizeCacheRef.current.set(change.id, {
          width: change.dimensions.width,
          height: change.dimensions.height,
        })
        gotDimensions = true
      }
    })
    if (gotDimensions) setMeasurementVersion(v => v + 1)
    onNodesChange(changes)
  }, [onNodesChange])

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
        <div style={{ opacity: layoutReady ? 1 : 0, transition: 'opacity 0.15s ease', height: '100%' }}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={handleNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeMouseEnter={onNodeMouseEnter}
            onNodeMouseLeave={onNodeMouseLeave}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            nodesDraggable={nodesDraggable}
            nodesConnectable={false}
            minZoom={minZoom}
            maxZoom={maxZoom}
            zoomOnScroll={false}
            panOnScroll={true}
          >
            <LayoutController
              rawBuild={filteredRawBuild}
              structuralVersion={structuralVersion}
              dataVersion={dataVersion}
              measurementVersion={measurementVersion}
              layoutOptions={mergedLayoutOptions}
              finalizeNodes={finalizeNodes}
              setNodes={setNodes}
              setEdges={setEdges}
              onLayoutDone={handleLayoutDone}
              containerHeight={height}
              fitViewAlign={fitViewAlign}
              nodeSizeCache={nodeSizeCacheRef}
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
        </div>
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
