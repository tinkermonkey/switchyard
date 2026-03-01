import { useEffect, useRef, useCallback } from 'react'
import { useNodesInitialized, useReactFlow, useStoreApi } from '@xyflow/react'
import { applyCycleLayout, updateEdgesForCycles } from '../utils/cycleLayout'

/**
 * Null-rendering component that must be placed inside a <ReactFlow> provider.
 *
 * Implements two-phase measured layout:
 *   Phase 1 (parent): sets rawBuild.nodes at {0,0} → React Flow renders & measures them
 *   Phase 2 (here):   reads node.measured dimensions → runs applyCycleLayout → sets final nodes
 *
 * Props:
 *   rawBuild        { nodes, edges, agentExecutions, updatedCycles } from buildFlowchart
 *   layoutOptions   params object passed to applyCycleLayout
 *   finalizeNodes   (layoutedNodes) => finalNodes  — caller injects draggable/callbacks
 *   setNodes        from useNodesState
 *   setEdges        from useEdgesState
 *   onLayoutDone    optional (finalNodes) => void  — called after each layout pass
 *   containerHeight optional — when this changes after initial layout, fitView is re-called
 *                   so the graph stays in view after a container resize.
 *   fitViewAlign    'center' | 'top' | 'bottom' | 'active-node' (default 'center')
 *                   Controls the initial viewport framing after layout:
 *                     'top'         — aligns to the top of the graph (first events visible);
 *                                     zoom is set to fit the full graph width.
 *                     'bottom'      — aligns to the bottom of the graph (most recent events);
 *                                     same width-fitting zoom.
 *                     'center'      — standard React Flow fitView (centers all content).
 *                     'active-node' — centres on the node with data.isActive === true;
 *                                     falls back to 'bottom' if no active node is found.
 */
export default function LayoutController({
  rawBuild,
  structuralVersion,
  layoutOptions,
  finalizeNodes,
  setNodes,
  setEdges,
  onLayoutDone,
  containerHeight,
  fitViewAlign = 'center',
}) {
  const storeApi = useStoreApi()
  const { getNodes, setViewport, fitView } = useReactFlow()
  const nodesInitialized = useNodesInitialized()
  const layoutDone = useRef(false)
  const isFirstLayout = useRef(true)

  // Stable refs — let callbacks read current values without being recreated
  const rawBuildRef = useRef(rawBuild)
  const layoutOptionsRef = useRef(layoutOptions)
  const finalizeNodesRef = useRef(finalizeNodes)
  const onLayoutDoneRef = useRef(onLayoutDone)
  const fitViewAlignRef = useRef(fitViewAlign)
  const lastFinalNodesRef = useRef([])

  useEffect(() => { rawBuildRef.current = rawBuild }, [rawBuild])
  useEffect(() => { layoutOptionsRef.current = layoutOptions }, [layoutOptions])
  useEffect(() => { finalizeNodesRef.current = finalizeNodes }, [finalizeNodes])
  useEffect(() => { onLayoutDoneRef.current = onLayoutDone }, [onLayoutDone])
  useEffect(() => { fitViewAlignRef.current = fitViewAlign }, [fitViewAlign])

  /**
   * Positions the viewport after layout based on fitViewAlign.
   *
   * 'top' / 'bottom': fits the graph width into the container (zoom) and aligns
   * the viewport to the top or bottom of the root-node bounding box. The bounding
   * box is derived from root-level nodes (absolute positions) only — child nodes
   * are positioned relative to their parent so they don't need separate handling.
   *
   * 'center': delegates to React Flow's built-in fitView.
   */
  const applyAlignedFitView = useCallback((nodes, duration = 300) => {
    const align = fitViewAlignRef.current

    if (!align || align === 'center') {
      fitView({ padding: 0.1, duration })
      return
    }

    // Centre on the in-progress node if one exists; fall through to 'bottom' if not.
    if (align === 'active-node') {
      const activeNode = nodes.find(n => n.data?.isActive)
      if (activeNode) {
        fitView({ nodes: [{ id: activeNode.id }], padding: 0.5, maxZoom: 1, duration })
        return
      }
    }

    // Only root-level nodes have absolute positions in the canvas
    const rootNodes = nodes.filter(n => !n.parentId)
    if (rootNodes.length === 0) {
      fitView({ padding: 0.1, duration })
      return
    }

    const { width: containerW, height: containerH } = storeApi.getState()
    if (!containerW || !containerH) {
      fitView({ padding: 0.1, duration })
      return
    }

    const padding = 40

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
    rootNodes.forEach(node => {
      const w = node.style?.width ?? node.measured?.width ?? 250
      const h = node.style?.height ?? node.measured?.height ?? 80
      minX = Math.min(minX, node.position.x)
      maxX = Math.max(maxX, node.position.x + w)
      minY = Math.min(minY, node.position.y)
      maxY = Math.max(maxY, node.position.y + h)
    })

    const graphWidth = maxX - minX
    if (graphWidth <= 0) {
      fitView({ padding: 0.1, duration })
      return
    }

    // Zoom to fit the full graph width, clamped to sane limits
    const zoom = Math.max(0.3, Math.min(2, (containerW - padding * 2) / graphWidth))
    // Horizontally center the graph
    const x = (containerW - graphWidth * zoom) / 2 - minX * zoom
    // Vertically align to top or bottom ('active-node' fallthrough treats as 'bottom')
    const y = (align === 'bottom' || align === 'active-node')
      ? containerH - padding - maxY * zoom
      : padding - minY * zoom

    setViewport({ x, y, zoom }, { duration })
  }, [fitView, setViewport, storeApi])

  // Core layout runner — stable identity, reads mutable state via refs
  const runLayout = useCallback(() => {
    const rb = rawBuildRef.current
    if (!rb || rb.nodes.length === 0) return
    const measuredNodes = getNodes()
    if (measuredNodes.length === 0) return

    // Guard: ensure measured nodes belong to the current rawBuild.
    // If rawBuild changed but Phase 1 hasn't run yet, getNodes() still returns
    // the previous layout's nodes — bail out so the correct Phase 2 fires later.
    const rawNodeIds = new Set(rb.nodes.map(n => n.id))
    if (measuredNodes.length !== rawNodeIds.size || !measuredNodes.every(n => rawNodeIds.has(n.id))) {
      return
    }

    const { nodes: layoutedNodes } = applyCycleLayout(
      measuredNodes, rb.edges, rb.updatedCycles, layoutOptionsRef.current
    )
    const finalNodes = finalizeNodesRef.current(layoutedNodes)
    const finalEdges = updateEdgesForCycles(rb.edges, rb.updatedCycles, rb.agentExecutions)

    setNodes(finalNodes)
    setEdges(finalEdges)
    layoutDone.current = true
    lastFinalNodesRef.current = finalNodes
    onLayoutDoneRef.current?.(finalNodes)
    const duration = isFirstLayout.current ? 0 : 300
    isFirstLayout.current = false
    setTimeout(() => applyAlignedFitView(finalNodes, duration), 50)
  }, [getNodes, setNodes, setEdges, applyAlignedFitView])

  // Reset layout flag on structural changes (new/removed nodes) so Phase 2 re-runs.
  // isFirstLayout is intentionally NOT reset here — it stays false after the first
  // layout so all subsequent layouts animate (300ms) rather than snapping (0ms).
  // IMPORTANT: this effect must be declared before the data-only edge effect below —
  // both fire in the same commit when rawBuild and structuralVersion change together,
  // and the data-only effect relies on reading layoutDone.current === false.
  useEffect(() => {
    layoutDone.current = false
  }, [structuralVersion])

  // Data-only update: edges may carry agent execution state that changes without
  // structural changes. Keep them current without re-running the full layout.
  // Reads layoutDone.current at runtime — intentionally non-reactive; relies on the
  // [structuralVersion] effect above having already set it to false for structural changes.
  useEffect(() => {
    if (!rawBuild || !layoutDone.current) return
    const finalEdges = updateEdgesForCycles(rawBuild.edges, rawBuild.updatedCycles, rawBuild.agentExecutions)
    setEdges(finalEdges)
  }, [rawBuild, setEdges])

  // Phase 2: all current nodes measured → apply layout.
  // structuralVersion is included so this re-evaluates when new nodes are added to an
  // already-visible graph (nodesInitialized may stay true across the update, meaning
  // the dependency would not change without structuralVersion).
  useEffect(() => {
    if (!nodesInitialized || layoutDone.current) return
    runLayout()
  }, [nodesInitialized, runLayout, structuralVersion])

  // Re-layout when layout params change (nodes are already rendered and measured)
  useEffect(() => {
    if (!rawBuildRef.current || !layoutDone.current) return
    runLayout()
  }, [layoutOptions, runLayout])

  // Re-fit view when the container is resized after layout.
  useEffect(() => {
    if (!layoutDone.current) return
    setTimeout(() => applyAlignedFitView(lastFinalNodesRef.current), 50)
  }, [containerHeight, applyAlignedFitView])

  return null
}
