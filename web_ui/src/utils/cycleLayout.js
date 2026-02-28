/**
 * Custom layout algorithm for pipeline runs.
 *
 * Positions nodes using a 3-level hierarchy:
 *   Root level  → reviewCycleContainer / repairCycleContainer / standalone pipelineEvent nodes
 *   Level 2     → iterationContainer nodes (children of cycle containers)
 *   Level 3     → pipelineEvent grandchildren inside iteration containers
 *
 * Entry points used by the rest of the app:
 *   applyCycleLayout      — main layout function, called by LayoutController
 *   toggleCycleCollapsed  — collapses/expands a cycle container
 *   updateEdgesForCycles  — redirects edges when cycles are collapsed
 */

// Debug logging control - set to true to enable verbose console logging
const DEBUG_CYCLE_LAYOUT = false

/**
 * Positions pipeline nodes using a 3-level layout hierarchy:
 *   Root level  → reviewCycleContainer / repairCycleContainer / standalone pipelineEvent nodes
 *   Level 2     → iterationContainer nodes (children of cycle containers)
 *   Level 3     → pipelineEvent grandchildren inside iteration containers
 *
 * @param {Array}  nodes   - React Flow nodes (may have parentId set)
 * @param {Array}  edges   - React Flow edges
 * @param {Map}    cycles  - Cycle collapse state map (used by updateEdgesForCycles)
 * @param {Object} options - Layout parameters (see DEFAULT_LAYOUT_OPTIONS in PipelineFlowGraph.jsx)
 * @returns {{ nodes, cycleNodes, edges }}
 */
export function applyCycleLayout(nodes, edges, cycles, options = {}) {
  const {
    nodeWidth = 250,
    nodeHeight = 80,
    horizontalSpacing = 150,
    verticalSpacing = 120,
    cycleGap = 100,
    cyclePadding = 40,
    // Iteration / grandchild layout constants
    iterHeaderHeight = 24, // height of the iteration pill label
    iterPadding = 20,      // padding inside iteration container (top & bottom)
    innerVertSpacing = 20, // vertical gap between grandchildren
    containerHeaderHeight = 36, // height of cycle container header bar
    viewportWidth = 1200,
    centerX = null,
  } = options

  const centerXPosition = centerX !== null ? centerX : viewportWidth / 2

  // Categorise nodes by type and parent relationship
  const cycleContainers = nodes.filter(
    n => (n.type === 'reviewCycleContainer' || n.type === 'repairCycleContainer') && !n.parentId
  )
  const iterContainers = nodes.filter(n => n.type === 'iterationContainer')
  const grandchildren = nodes.filter(n => n.parentId && n.type === 'pipelineEvent' &&
    iterContainers.some(ic => ic.id === n.parentId))
  // Direct pipelineEvent children of cycle containers (review_cycle_started / completed)
  const directCycleChildren = nodes.filter(n => n.parentId && n.type === 'pipelineEvent' &&
    cycleContainers.some(cc => cc.id === n.parentId))

  // Build lookup maps
  const itersByParent = new Map()   // cycleId → [iterationContainer]
  iterContainers.forEach(iter => {
    const pid = iter.parentId
    if (!itersByParent.has(pid)) itersByParent.set(pid, [])
    itersByParent.get(pid).push(iter)
  })

  const childrenByIter = new Map()  // iterId → [grandchild pipelineEvent]
  grandchildren.forEach(child => {
    const pid = child.parentId
    if (!childrenByIter.has(pid)) childrenByIter.set(pid, [])
    childrenByIter.get(pid).push(child)
  })

  const directChildrenByCycle = new Map()  // cycleId → [pipelineEvent]
  directCycleChildren.forEach(child => {
    const pid = child.parentId
    if (!directChildrenByCycle.has(pid)) directChildrenByCycle.set(pid, [])
    directChildrenByCycle.get(pid).push(child)
  })

  // ── Pass 1: Size iteration / test-cycle containers (bottom-up) ───────────
  // Uses node.measured dimensions when available (two-phase layout), falls back to params.
  const iterSizes = new Map()
  iterContainers.forEach(iter => {
    const children = childrenByIter.get(iter.id) || []
    const n = children.length
    const totalChildHeight = children.reduce(
      (sum, c) => sum + (c.measured?.height ?? nodeHeight), 0
    )
    const maxChildWidth = n > 0
      ? Math.max(...children.map(c => c.measured?.width ?? nodeWidth))
      : nodeWidth
    const height = iterHeaderHeight + iterPadding * 2 +
      totalChildHeight +
      Math.max(0, n - 1) * innerVertSpacing
    const width = maxChildWidth + iterPadding * 2
    iterSizes.set(iter.id, { width, height })
  })

  // ── Pass 2: Size cycle containers ────────────────────────────────────────
  const cycleSizes = new Map()
  cycleContainers.forEach(cc => {
    const iters = itersByParent.get(cc.id) || []
    const direct = directChildrenByCycle.get(cc.id) || []
    const maxIterHeight = iters.reduce(
      (max, it) => Math.max(max, iterSizes.get(it.id)?.height ?? 0), nodeHeight
    )

    if (cc.type === 'reviewCycleContainer') {
      // Layout: [start] [iter1] [iter2] … [end]  — all horizontal
      const numDirect = direct.length   // typically 2 (start + end events)
      const numIters = iters.length
      const iterTotalWidth = iters.reduce(
        (sum, it) => sum + (iterSizes.get(it.id)?.width ?? 0), 0
      )
      // Use measured widths for direct children (start/end events) when available
      const startWidth = direct[0]?.measured?.width ?? nodeWidth
      const endWidth = direct[1]?.measured?.width ?? nodeWidth
      const leftWidth = numDirect > 0 ? startWidth + horizontalSpacing : 0
      const rightWidth = numDirect > 1 ? horizontalSpacing + endWidth : 0
      const iterSpacingTotal = numIters > 1 ? horizontalSpacing * (numIters - 1) : 0
      const width = cyclePadding * 2 + leftWidth + iterTotalWidth + iterSpacingTotal + rightWidth
      const height = containerHeaderHeight + cyclePadding * 2 + maxIterHeight
      cycleSizes.set(cc.id, { width: Math.max(width, 500), height: Math.max(height, 180) })
    } else if (cc.type === 'repairCycleContainer') {
      // Layout: [tc1] [tc2] [tc3]  — horizontal, no direct event children
      const numIters = iters.length
      const iterTotalWidth = iters.reduce(
        (sum, it) => sum + (iterSizes.get(it.id)?.width ?? 0), 0
      )
      const iterSpacingTotal = numIters > 1 ? horizontalSpacing * (numIters - 1) : 0
      const width = cyclePadding * 2 + iterTotalWidth + iterSpacingTotal
      const height = containerHeaderHeight + cyclePadding * 2 + maxIterHeight
      cycleSizes.set(cc.id, { width: Math.max(width, 400), height: Math.max(height, 180) })
    }
  })

  // ── Pass 3: Position root-level items vertically ─────────────────────────
  // Root items = all nodes without parentId, in the order they appear in the array
  // (buildFlowchart.js inserts them in chronological order)
  // Uses node.measured dimensions when available (two-phase layout).
  const positionedNodes = new Map()  // id → fully positioned node
  const nodeGap = Math.max(16, verticalSpacing - nodeHeight)  // gap between consecutive nodes

  // Process all root-level nodes (no parentId) in array order, which is chronological
  const rootLayoutNodes = nodes.filter(n => !n.parentId)
  let currentY = 100
  rootLayoutNodes.forEach(node => {
    if (node.type === 'reviewCycleContainer' || node.type === 'repairCycleContainer') {
      const size = cycleSizes.get(node.id) || { width: 500, height: 200 }
      positionedNodes.set(node.id, {
        ...node,
        position: { x: centerXPosition - size.width / 2, y: currentY },
        style: { ...node.style, width: size.width, height: size.height },
      })
      currentY += size.height + cycleGap
    } else {
      const w = node.measured?.width ?? nodeWidth
      const h = node.measured?.height ?? nodeHeight
      positionedNodes.set(node.id, {
        ...node,
        position: { x: centerXPosition - w / 2, y: currentY },
      })
      currentY += h + nodeGap
    }
  })

  // ── Pass 4: Position cycle container children ─────────────────────────────
  cycleContainers.forEach(cc => {
    const iters = (itersByParent.get(cc.id) || []).sort(
      (a, b) => (a.data?.iterationNumber ?? 0) - (b.data?.iterationNumber ?? 0)
    )
    const direct = (directChildrenByCycle.get(cc.id) || []).sort(
      (a, b) => new Date(a.data?.timestamp ?? 0) - new Date(b.data?.timestamp ?? 0)
    )

    const contentY = containerHeaderHeight + cyclePadding  // y offset inside container

    if (cc.type === 'reviewCycleContainer') {
      let relX = cyclePadding

      // Start event (leftmost direct child)
      if (direct[0]) {
        positionedNodes.set(direct[0].id, {
          ...direct[0],
          position: { x: relX, y: contentY },
        })
        relX += (direct[0].measured?.width ?? nodeWidth) + horizontalSpacing
      }

      // Iteration containers
      iters.forEach(iter => {
        const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2, height: 200 }
        positionedNodes.set(iter.id, {
          ...iter,
          position: { x: relX, y: contentY },
          style: { ...iter.style, width: iterSize.width, height: iterSize.height },
        })
        relX += iterSize.width + horizontalSpacing
      })

      // End event (rightmost direct child)
      if (direct[1]) {
        positionedNodes.set(direct[1].id, {
          ...direct[1],
          position: { x: relX, y: contentY },
        })
      }
    } else if (cc.type === 'repairCycleContainer') {
      let relX = cyclePadding

      iters.forEach(iter => {
        const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2, height: 200 }
        positionedNodes.set(iter.id, {
          ...iter,
          position: { x: relX, y: contentY },
          style: { ...iter.style, width: iterSize.width, height: iterSize.height },
        })
        relX += iterSize.width + horizontalSpacing
      })
    }
  })

  // ── Pass 5: Position grandchildren within iteration containers ────────────
  // Uses cumulative measured heights for accurate vertical stacking.
  // Children are horizontally centered within the iteration container.
  iterContainers.forEach(iter => {
    const children = childrenByIter.get(iter.id) || []
    const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2 }
    let childY = iterHeaderHeight + iterPadding
    // Preserve insertion order (chronological — buildFlowchart.js inserts them that way)
    children.forEach(child => {
      const childWidth = child.measured?.width ?? nodeWidth
      const centeredX = (iterSize.width - childWidth) / 2
      positionedNodes.set(child.id, {
        ...child,
        position: { x: centeredX, y: childY },
      })
      childY += (child.measured?.height ?? nodeHeight) + innerVertSpacing
    })
  })

  // ── Collect and order final nodes (parents before children) ──────────────
  const parentNodes = nodes.filter(n => !n.parentId).map(n => positionedNodes.get(n.id) ?? n)
  const childNodes = nodes.filter(n => n.parentId).map(n => positionedNodes.get(n.id) ?? n)

  const cycleNodes = nodes.filter(
    n => n.type === 'reviewCycleContainer' || n.type === 'repairCycleContainer'
  ).map(n => positionedNodes.get(n.id) ?? n)

  return {
    nodes: [...parentNodes, ...childNodes],
    cycleNodes,
    edges,
  }
}

/**
 * Toggles the collapsed state of a cycle
 * @param {Map} cycles - Map of cycles
 * @param {String} cycleId - Cycle ID to toggle
 * @returns {Map} Updated cycles map
 */
export function toggleCycleCollapsed(cycles, cycleId) {
  const updatedCycles = new Map(cycles)
  const cycleData = updatedCycles.get(cycleId)
  
  if (cycleData) {
    updatedCycles.set(cycleId, {
      ...cycleData,
      isCollapsed: !cycleData.isCollapsed,
    })
  }
  
  return updatedCycles
}

/**
 * Updates edges to connect to cycle bounding nodes when collapsed
 * @param {Array} edges - All edges
 * @param {Map} cycles - Map of cycles
 * @param {Map} nodesByCycle - Map of agent -> [nodes]
 * @returns {Array} Updated edges
 */
export function updateEdgesForCycles(edges, cycles, nodesByCycle) {
  const updatedEdges = []
  
  edges.forEach(edge => {
    let newEdge = { ...edge }
    
    // Check if source is in a collapsed cycle
    for (const [agent, cycleData] of cycles.entries()) {
      if (cycleData.isCollapsed) {
        const cyclePrefix = `agent-${agent}-`
        
        if (edge.source.startsWith(cyclePrefix)) {
          // Redirect to cycle bounding node
          newEdge = {
            ...newEdge,
            source: `cycle-${agent}`,
          }
        }
        
        if (edge.target.startsWith(cyclePrefix)) {
          // Redirect to cycle bounding node
          newEdge = {
            ...newEdge,
            target: `cycle-${agent}`,
          }
        }
      }
    }
    
    updatedEdges.push(newEdge)
  })
  
  // Remove duplicate edges (multiple iterations connecting to same external nodes)
  const edgeKeys = new Set()
  return updatedEdges.filter(edge => {
    const key = `${edge.source}->${edge.target}`
    if (edgeKeys.has(key)) {
      return false
    }
    edgeKeys.add(key)
    return true
  })
}
