import { useEffect, useRef, useCallback } from 'react'
import { useNodesInitialized, useReactFlow } from '@xyflow/react'
import { applyCycleLayout, updateEdgesForCycles } from '../utils/cycleLayout'

/**
 * Null-rendering component that must be placed inside a <ReactFlow> provider.
 *
 * Implements two-phase measured layout:
 *   Phase 1 (parent): sets rawBuild.nodes at {0,0} → React Flow renders & measures them
 *   Phase 2 (here):   reads node.measured dimensions → runs applyCycleLayout → sets final nodes
 *
 * Props:
 *   rawBuild      { nodes, edges, agentExecutions, updatedCycles } from buildFlowchart
 *   layoutOptions  params object passed to applyCycleLayout
 *   finalizeNodes  (layoutedNodes) => finalNodes  — caller injects draggable/callbacks
 *   setNodes       from useNodesState
 *   setEdges       from useEdgesState
 *   onLayoutDone   optional (finalNodes) => void  — called after each layout pass
 */
export default function LayoutController({
  rawBuild,
  layoutOptions,
  finalizeNodes,
  setNodes,
  setEdges,
  onLayoutDone,
}) {
  const { getNodes, fitView } = useReactFlow()
  const nodesInitialized = useNodesInitialized()
  const layoutDone = useRef(false)

  // Stable refs — let runLayout read current values without being recreated
  const rawBuildRef = useRef(rawBuild)
  const layoutOptionsRef = useRef(layoutOptions)
  const finalizeNodesRef = useRef(finalizeNodes)
  const onLayoutDoneRef = useRef(onLayoutDone)

  useEffect(() => { rawBuildRef.current = rawBuild }, [rawBuild])
  useEffect(() => { layoutOptionsRef.current = layoutOptions }, [layoutOptions])
  useEffect(() => { finalizeNodesRef.current = finalizeNodes }, [finalizeNodes])
  useEffect(() => { onLayoutDoneRef.current = onLayoutDone }, [onLayoutDone])

  // Core layout runner — stable identity, reads mutable state via refs
  const runLayout = useCallback(() => {
    const rb = rawBuildRef.current
    if (!rb || rb.nodes.length === 0) return
    const measuredNodes = getNodes()
    if (measuredNodes.length === 0) return

    const { nodes: layoutedNodes } = applyCycleLayout(
      measuredNodes, rb.edges, rb.updatedCycles, layoutOptionsRef.current
    )
    const finalNodes = finalizeNodesRef.current(layoutedNodes)
    const finalEdges = updateEdgesForCycles(rb.edges, rb.updatedCycles, rb.agentExecutions)

    setNodes(finalNodes)
    setEdges(finalEdges)
    layoutDone.current = true
    onLayoutDoneRef.current?.(finalNodes)
    setTimeout(() => fitView({ padding: 0.1, duration: 300 }), 50)
  }, [getNodes, setNodes, setEdges, fitView])

  // Reset layout flag whenever rawBuild changes (parent has just set new raw nodes)
  useEffect(() => {
    layoutDone.current = false
  }, [rawBuild])

  // Phase 2: all current nodes measured → apply layout
  useEffect(() => {
    if (!nodesInitialized || layoutDone.current) return
    runLayout()
  }, [nodesInitialized, runLayout])

  // Re-layout when layout params change (nodes are already rendered and measured)
  useEffect(() => {
    if (!rawBuildRef.current || !layoutDone.current) return
    runLayout()
  }, [layoutOptions, runLayout])

  return null
}
