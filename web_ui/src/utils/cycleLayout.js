/**
 * Custom layout algorithm for pipeline runs.
 *
 * Positions nodes using a 3–4 level hierarchy:
 *   Root level  → reviewCycleContainer / repairCycleContainer / standalone event nodes
 *   Level 2     → iterationContainer nodes (children of cycle containers)
 *   Level 3     → event grandchildren inside iteration containers; subCycleContainer (repair cycles)
 *   Level 4     → event leaves inside subCycleContainers (repair cycles only)
 *
 * Entry points used by the rest of the app:
 *   applyCycleLayout      — main layout function, called by LayoutController
 *   toggleCycleCollapsed  — collapses/expands a cycle container
 *   updateEdgesForCycles  — redirects edges when cycles are collapsed
 */

/**
 * Node types that are structural containers, not renderable pipeline events.
 * Any node whose type is NOT in this set is treated as a leaf event node by the layout engine.
 * This replaces the previous hard-coded `n.type === 'pipelineEvent'` checks, which broke
 * when the event nodes were given granular per-event-type strings.
 */
const CONTAINER_TYPES = new Set([
  'reviewCycleContainer',
  'repairCycleContainer',
  'prReviewCycleContainer',
  'conversationalLoopContainer',
  'iterationContainer',
  'subCycleContainer',
  'cycleBounding',
])

// Debug logging control - set to true to enable verbose console logging
const DEBUG_CYCLE_LAYOUT = false

/**
 * Groups sub-cycle and direct-child nodes into per-iteration columns.
 * Returns null if there are no subCycles (callers fall back to vertical layout).
 * Returns Map<iterationNumber, { items: [{node, h, w}], totalH, maxW }>
 *
 * @param {Array}  subCycles      - subCycleContainer nodes for this iteration container
 * @param {Array}  directChildren - direct pipelineEvent grandchildren (residuals)
 * @param {Map}    subCycleSizes  - subCycleId → {width, height}
 * @param {Object} opts           - { nodeWidth, nodeHeight, innerVertSpacing }
 */
function groupIterationColumns(subCycles, directChildren, subCycleSizes, opts) {
  const { nodeWidth, nodeHeight, innerVertSpacing } = opts
  if (!subCycles.length) return null

  // Unique iteration numbers derived from sub-cycles, sorted ascending
  const iterNums = [...new Set(subCycles.map(sc => sc.data.iterationNumber))].sort((a, b) => a - b)

  // Earliest start and latest end timestamp per column (for direct-child assignment)
  const colStartMs = new Map()
  const colEndMs = new Map()
  iterNums.forEach(n => {
    const scForN = subCycles.filter(sc => sc.data.iterationNumber === n)
    const minStart = Math.min(...scForN.map(sc => new Date(sc.data.startEvent?.timestamp || 0).getTime()))
    const maxEnd = Math.max(...scForN.map(sc =>
      sc.data.endEvent ? new Date(sc.data.endEvent.timestamp).getTime()
                       : new Date(sc.data.startEvent?.timestamp || 0).getTime()
    ))
    colStartMs.set(n, minStart)
    colEndMs.set(n, maxEnd)
  })

  // Build columns: each column holds sub-cycles for that iteration number
  const columns = new Map()
  iterNums.forEach(n => {
    const scForN = subCycles
      .filter(sc => sc.data.iterationNumber === n)
      .sort((a, b) =>
        new Date(a.data.startEvent?.timestamp || 0) - new Date(b.data.startEvent?.timestamp || 0)
      )
    const items = scForN.map(sc => {
      const sz = subCycleSizes.get(sc.id) || { width: nodeWidth, height: nodeHeight }
      return { node: sc, h: sz.height, w: sz.width }
    })
    columns.set(n, { items })
  })

  // Assign residual direct children to columns.
  // Events within a column's active span (with 60s look-behind for preamble events) go to that
  // column. Events in the gap between column N's end and column N+1's start are "opener" events
  // for iteration N+1 (e.g. repair_cycle_iteration markers) and belong to column N+1.
  directChildren.forEach(child => {
    const childMs = new Date(child.data?.timestamp || child.data?.startTime || 0).getTime()
    let assignedCol = iterNums[iterNums.length - 1] // default: last column
    for (let i = 0; i < iterNums.length; i++) {
      const n = iterNums[i]
      const nextN = i + 1 < iterNums.length ? iterNums[i + 1] : null
      const thisStart = colStartMs.get(n)
      const thisEnd = colEndMs.get(n)
      const nextStart = nextN !== null ? colStartMs.get(nextN) : Infinity
      // Within this column's active time span (60s look-behind covers preamble events)
      if (childMs >= thisStart - 60_000 && childMs <= thisEnd) {
        assignedCol = n
        break
      }
      // In the gap between this column and the next → opener for the next iteration
      if (nextN !== null && childMs > thisEnd && childMs < nextStart) {
        assignedCol = nextN
        break
      }
    }
    const col = columns.get(assignedCol)
    if (col) {
      col.items.push({
        node: child,
        h: child.measured?.height ?? nodeHeight,
        w: child.measured?.width ?? nodeWidth,
      })
    }
  })

  // Compute totalH and maxW per column
  columns.forEach(col => {
    const { items } = col
    col.totalH =
      items.reduce((sum, item) => sum + item.h, 0) +
      Math.max(0, items.length - 1) * innerVertSpacing
    col.maxW = items.length > 0 ? Math.max(...items.map(item => item.w)) : nodeWidth
  })

  return columns
}

/**
 * Positions pipeline nodes using a 3–4 level layout hierarchy:
 *   Root level  → reviewCycleContainer / repairCycleContainer / standalone event nodes
 *   Level 2     → iterationContainer nodes (children of cycle containers)
 *   Level 3     → event grandchildren inside iteration containers; subCycleContainer (repair cycles)
 *   Level 4     → event leaves inside subCycleContainers (repair cycles only)
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
    innerHorizSpacing = 60, // horizontal gap between iteration columns
    containerHeaderHeight = 36, // height of cycle container header bar
    viewportWidth = 1200,
    centerX = null,
  } = options

  const centerXPosition = centerX !== null ? centerX : viewportWidth / 2
  const _clT0 = performance.now()

  // Categorise nodes by type and parent relationship
  const cycleContainers = nodes.filter(
    n => (
      n.type === 'reviewCycleContainer' ||
      n.type === 'repairCycleContainer' ||
      n.type === 'prReviewCycleContainer' ||
      n.type === 'conversationalLoopContainer'
    ) && !n.parentId
  )
  const iterContainers = nodes.filter(n => n.type === 'iterationContainer')
  const subCycleContainers = nodes.filter(n => n.type === 'subCycleContainer')

  // Build ID sets for O(1) parent lookups instead of O(n) .some() scans
  const iterContainerIds = new Set(iterContainers.map(n => n.id))
  const cycleContainerIds = new Set(cycleContainers.map(n => n.id))
  const subCycleContainerIds = new Set(subCycleContainers.map(n => n.id))

  // Leaf event children of iterationContainers (residual events not inside sub-cycles)
  const grandchildren = nodes.filter(n => n.parentId && !CONTAINER_TYPES.has(n.type) &&
    iterContainerIds.has(n.parentId))
  // Leaf event children of cycle containers (e.g. reviewCycleStarted / reviewCycleCompleted)
  const directCycleChildren = nodes.filter(n => n.parentId && !CONTAINER_TYPES.has(n.type) &&
    cycleContainerIds.has(n.parentId))
  // Leaf event children of subCycleContainers
  const subCycleLeaves = nodes.filter(n => n.parentId && !CONTAINER_TYPES.has(n.type) &&
    subCycleContainerIds.has(n.parentId))

  // Build lookup maps
  const itersByParent = new Map()   // cycleId → [iterationContainer]
  iterContainers.forEach(iter => {
    const pid = iter.parentId
    if (!itersByParent.has(pid)) itersByParent.set(pid, [])
    itersByParent.get(pid).push(iter)
  })

  const childrenByIter = new Map()  // iterId → [direct pipelineEvent grandchild]
  grandchildren.forEach(child => {
    const pid = child.parentId
    if (!childrenByIter.has(pid)) childrenByIter.set(pid, [])
    childrenByIter.get(pid).push(child)
  })

  const subCyclesByIter = new Map()  // iterId → [subCycleContainer]
  subCycleContainers.forEach(sc => {
    const pid = sc.parentId
    if (!subCyclesByIter.has(pid)) subCyclesByIter.set(pid, [])
    subCyclesByIter.get(pid).push(sc)
  })

  const leavesBySubCycle = new Map()  // subCycleId → [pipelineEvent]
  subCycleLeaves.forEach(leaf => {
    const pid = leaf.parentId
    if (!leavesBySubCycle.has(pid)) leavesBySubCycle.set(pid, [])
    leavesBySubCycle.get(pid).push(leaf)
  })

  const directChildrenByCycle = new Map()  // cycleId → [pipelineEvent]
  directCycleChildren.forEach(child => {
    const pid = child.parentId
    if (!directChildrenByCycle.has(pid)) directChildrenByCycle.set(pid, [])
    directChildrenByCycle.get(pid).push(child)
  })

  // Identify iterationContainers that are children of repairCycleContainers
  // (these are test-cycle containers whose sub-cycles should use column layout)
  const repairCycleIds = new Set(
    cycleContainers.filter(cc => cc.type === 'repairCycleContainer').map(cc => cc.id)
  )
  const repairTestCycleIds = new Set(
    iterContainers.filter(ic => repairCycleIds.has(ic.parentId)).map(ic => ic.id)
  )

  const _clT1 = performance.now() // categorise + lookup maps done

  // ── Pass 0: Size subCycleContainers (bottom-up) ───────────────────────────
  const subCycleSizes = new Map()
  subCycleContainers.forEach(sc => {
    const leaves = leavesBySubCycle.get(sc.id) || []
    const n = leaves.length
    const totalLeafH = leaves.reduce((sum, c) => sum + (c.measured?.height ?? nodeHeight), 0)
    const maxLeafW = n > 0 ? Math.max(...leaves.map(c => c.measured?.width ?? nodeWidth)) : nodeWidth
    const height = iterHeaderHeight + iterPadding * 2 + totalLeafH + Math.max(0, n - 1) * innerVertSpacing
    const width = maxLeafW + iterPadding * 2
    subCycleSizes.set(sc.id, { width, height })
  })

  const _clT2 = performance.now() // pass 0 done

  // ── Pass 1: Size iteration / test-cycle containers (bottom-up) ───────────
  // Uses node.measured dimensions when available (two-phase layout), falls back to params.
  const iterSizes = new Map()
  iterContainers.forEach(iter => {
    const directChildren = childrenByIter.get(iter.id) || []
    const subCycles = subCyclesByIter.get(iter.id) || []

    // Repair test-cycle containers use horizontal column layout (one column per iteration number)
    if (repairTestCycleIds.has(iter.id)) {
      const columns = groupIterationColumns(subCycles, directChildren, subCycleSizes, {
        nodeWidth, nodeHeight, innerVertSpacing,
      })
      if (columns) {
        const colValues = [...columns.values()]
        const numColumns = colValues.length
        const totalColWidth = colValues.reduce((sum, col) => sum + col.maxW, 0)
        const maxColHeight = Math.max(...colValues.map(col => col.totalH), 0)
        const width = iterPadding * 2 + totalColWidth + (numColumns - 1) * innerHorizSpacing
        const height = iterHeaderHeight + iterPadding * 2 + maxColHeight
        iterSizes.set(iter.id, { width, height, columns })
        return
      }
    }

    // Default: vertical stack sizing
    const allChildren = [
      ...directChildren.map(c => ({ w: c.measured?.width ?? nodeWidth, h: c.measured?.height ?? nodeHeight })),
      ...subCycles.map(sc => {
        const sz = subCycleSizes.get(sc.id) || { width: nodeWidth, height: nodeHeight }
        return { w: sz.width, h: sz.height }
      }),
    ]

    const n = allChildren.length
    const totalH = allChildren.reduce((sum, c) => sum + c.h, 0)
    const maxW = n > 0 ? Math.max(...allChildren.map(c => c.w)) : nodeWidth
    const height = iterHeaderHeight + iterPadding * 2 + totalH + Math.max(0, n - 1) * innerVertSpacing
    const width = maxW + iterPadding * 2
    iterSizes.set(iter.id, { width, height })
  })

  const _clT3 = performance.now() // pass 1 done

  // ── Pass 2: Size cycle containers ────────────────────────────────────────
  const cycleSizes = new Map()
  cycleContainers.forEach(cc => {
    const iters = itersByParent.get(cc.id) || []
    const direct = directChildrenByCycle.get(cc.id) || []
    const maxIterHeight = iters.reduce(
      (max, it) => Math.max(max, iterSizes.get(it.id)?.height ?? 0), nodeHeight
    )

    if (cc.type === 'reviewCycleContainer' || cc.type === 'prReviewCycleContainer') {
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
      // Layout: [residual-col?] [tc1] [tc2] [tc3]  — horizontal
      const numIters = iters.length
      const iterTotalWidth = iters.reduce(
        (sum, it) => sum + (iterSizes.get(it.id)?.width ?? 0), 0
      )
      const iterSpacingTotal = numIters > 1 ? horizontalSpacing * (numIters - 1) : 0

      // Residual event column (direct event children outside any test cycle)
      const directMaxW = direct.length > 0
        ? Math.max(...direct.map(c => c.measured?.width ?? nodeWidth))
        : 0
      const directTotalH = direct.length > 0
        ? direct.reduce((sum, c) => sum + (c.measured?.height ?? nodeHeight), 0) +
          Math.max(0, direct.length - 1) * innerVertSpacing
        : 0
      const directColW = directMaxW > 0 ? directMaxW + horizontalSpacing : 0

      const width = cyclePadding * 2 + directColW + iterTotalWidth + iterSpacingTotal
      const height = containerHeaderHeight + cyclePadding * 2 + Math.max(maxIterHeight, directTotalH)
      cycleSizes.set(cc.id, { width: Math.max(width, 400), height: Math.max(height, 180) })
    } else if (cc.type === 'conversationalLoopContainer') {
      // Layout: vertical stack of direct event children (start + child events + end)
      const numDirect = direct.length
      const directTotalH = numDirect > 0
        ? direct.reduce((sum, c) => sum + (c.measured?.height ?? nodeHeight), 0) +
          Math.max(0, numDirect - 1) * innerVertSpacing
        : 0
      const directMaxW = numDirect > 0
        ? Math.max(...direct.map(c => c.measured?.width ?? nodeWidth))
        : nodeWidth
      const width = cyclePadding * 2 + directMaxW
      const height = containerHeaderHeight + cyclePadding * 2 + directTotalH
      cycleSizes.set(cc.id, { width: Math.max(width, 300), height: Math.max(height, 180) })
    }
  })

  const _clT4 = performance.now() // pass 2 done

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
    if (
      node.type === 'reviewCycleContainer' ||
      node.type === 'repairCycleContainer' ||
      node.type === 'prReviewCycleContainer' ||
      node.type === 'conversationalLoopContainer'
    ) {
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

  const _clT5 = performance.now() // pass 3 done

  // ── Pass 4: Position cycle container children ─────────────────────────────
  cycleContainers.forEach(cc => {
    const iters = (itersByParent.get(cc.id) || []).sort(
      (a, b) => (a.data?.iterationNumber ?? 0) - (b.data?.iterationNumber ?? 0)
    )
    const direct = (directChildrenByCycle.get(cc.id) || []).sort((a, b) => {
      // Decision nodes store time as data.timestamp; agentExecution nodes as data.startTime.
      const aTs = a.data?.timestamp ?? a.data?.startTime ?? 0
      const bTs = b.data?.timestamp ?? b.data?.startTime ?? 0
      return new Date(aTs) - new Date(bTs)
    })

    const contentY = containerHeaderHeight + cyclePadding  // y offset inside container

    if (cc.type === 'reviewCycleContainer' || cc.type === 'prReviewCycleContainer') {
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

      // Residual event column (direct event children outside any test cycle)
      if (direct.length > 0) {
        const maxW = Math.max(...direct.map(c => c.measured?.width ?? nodeWidth))
        let childY = contentY
        direct.forEach(child => {
          const childW = child.measured?.width ?? nodeWidth
          const childH = child.measured?.height ?? nodeHeight
          positionedNodes.set(child.id, {
            ...child,
            position: { x: relX + (maxW - childW) / 2, y: childY },
          })
          childY += childH + innerVertSpacing
        })
        relX += maxW + horizontalSpacing
      }

      iters.forEach(iter => {
        const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2, height: 200 }
        positionedNodes.set(iter.id, {
          ...iter,
          position: { x: relX, y: contentY },
          style: { ...iter.style, width: iterSize.width, height: iterSize.height },
        })
        relX += iterSize.width + horizontalSpacing
      })
    } else if (cc.type === 'conversationalLoopContainer') {
      // Vertical stack of all direct event children (start + child events + end)
      const ccSize = cycleSizes.get(cc.id) || { width: 300 }
      let childY = contentY
      direct.forEach(child => {
        const childW = child.measured?.width ?? nodeWidth
        const childH = child.measured?.height ?? nodeHeight
        positionedNodes.set(child.id, {
          ...child,
          position: { x: (ccSize.width - childW) / 2, y: childY },
        })
        childY += childH + innerVertSpacing
      })
    }
  })

  const _clT6 = performance.now() // pass 4 done

  // ── Pass 5: Position children of iteration containers (chronological order, mixed types) ─
  // Handles both direct pipelineEvent children (residuals) and subCycleContainers.
  iterContainers.forEach(iter => {
    const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2 }

    // Repair test-cycle containers: column layout (one column per iteration number)
    if (repairTestCycleIds.has(iter.id) && iterSize.columns) {
      let colX = iterPadding
      iterSize.columns.forEach(col => {
        let childY = iterHeaderHeight + iterPadding
        // Sort items within each column chronologically
        const sortedItems = [...col.items].sort((a, b) => {
          const aTs = a.node.data?.startEvent?.timestamp || a.node.data?.timestamp || a.node.data?.startTime || ''
          const bTs = b.node.data?.startEvent?.timestamp || b.node.data?.timestamp || b.node.data?.startTime || ''
          return new Date(aTs) - new Date(bTs)
        })
        sortedItems.forEach(({ node, h, w }) => {
          if (node.type === 'subCycleContainer') {
            const sz = subCycleSizes.get(node.id) || { width: w, height: h }
            positionedNodes.set(node.id, {
              ...node,
              position: { x: colX + (col.maxW - sz.width) / 2, y: childY },
              style: { ...node.style, width: sz.width, height: sz.height },
            })
          } else {
            positionedNodes.set(node.id, {
              ...node,
              position: { x: colX + (col.maxW - w) / 2, y: childY },
            })
          }
          childY += h + innerVertSpacing
        })
        colX += col.maxW + innerHorizSpacing
      })
      return
    }

    // Default: vertical stack layout
    const directChildren = childrenByIter.get(iter.id) || []
    const subCycles = subCyclesByIter.get(iter.id) || []

    // Sort all children chronologically
    const allChildren = [
      ...directChildren.map(c => ({ node: c, ts: c.data?.timestamp || c.data?.startTime || '' })),
      ...subCycles.map(sc => ({ node: sc, ts: sc.data?.startEvent?.timestamp || '' })),
    ].sort((a, b) => new Date(a.ts) - new Date(b.ts))

    let childY = iterHeaderHeight + iterPadding
    allChildren.forEach(({ node }) => {
      if (node.type === 'subCycleContainer') {
        const sz = subCycleSizes.get(node.id) || { width: nodeWidth, height: nodeHeight }
        const centeredX = (iterSize.width - sz.width) / 2
        positionedNodes.set(node.id, {
          ...node,
          position: { x: centeredX, y: childY },
          style: { ...node.style, width: sz.width, height: sz.height },
        })
        childY += sz.height + innerVertSpacing
      } else {
        const childW = node.measured?.width ?? nodeWidth
        const childH = node.measured?.height ?? nodeHeight
        positionedNodes.set(node.id, {
          ...node,
          position: { x: (iterSize.width - childW) / 2, y: childY },
        })
        childY += childH + innerVertSpacing
      }
    })
  })

  const _clT7 = performance.now() // pass 5 done

  // ── Pass 6: Position pipelineEvent leaves within subCycleContainers ───────
  subCycleContainers.forEach(sc => {
    const leaves = leavesBySubCycle.get(sc.id) || []
    const scSize = subCycleSizes.get(sc.id) || { width: nodeWidth + iterPadding * 2 }
    let leafY = iterHeaderHeight + iterPadding
    leaves.forEach(leaf => {
      const leafW = leaf.measured?.width ?? nodeWidth
      positionedNodes.set(leaf.id, {
        ...leaf,
        position: { x: (scSize.width - leafW) / 2, y: leafY },
      })
      leafY += (leaf.measured?.height ?? nodeHeight) + innerVertSpacing
    })
  })

  const _clT8 = performance.now() // pass 6 done

  // ── Collect and order final nodes (parents before children) ──────────────
  // Order: root → cycleContainers → iterContainers → direct children → subCycleContainers → subCycleLeaves
  const parentNodes = nodes.filter(n => !n.parentId).map(n => positionedNodes.get(n.id) ?? n)
  const iterContainerNodes = iterContainers.map(n => positionedNodes.get(n.id) ?? n)
  const directChildrenNodes = [...directCycleChildren, ...grandchildren].map(n => positionedNodes.get(n.id) ?? n)
  const subCycleContainerNodes = subCycleContainers.map(n => positionedNodes.get(n.id) ?? n)
  const subCycleLeafNodes = subCycleLeaves.map(n => positionedNodes.get(n.id) ?? n)

  const cycleNodes = nodes.filter(
    n => n.type === 'reviewCycleContainer' ||
         n.type === 'repairCycleContainer' ||
         n.type === 'prReviewCycleContainer' ||
         n.type === 'conversationalLoopContainer'
  ).map(n => positionedNodes.get(n.id) ?? n)

  const _clTEnd = performance.now()
  const _fmt = (a, b) => `${(b - a).toFixed(1)}ms`
  console.log(
    `[PerfGraph] applyCycleLayout: ${_fmt(_clT0, _clTEnd)}` +
    ` | nodes:${nodes.length} (cycle:${cycleContainers.length} iter:${iterContainers.length} sc:${subCycleContainers.length} leaf:${grandchildren.length + subCycleLeaves.length + directCycleChildren.length})` +
    ` | categorise:${_fmt(_clT0, _clT1)}` +
    ` | P0(scSize):${_fmt(_clT1, _clT2)}` +
    ` | P1(iterSize):${_fmt(_clT2, _clT3)}` +
    ` | P2(cycleSize):${_fmt(_clT3, _clT4)}` +
    ` | P3(rootPos):${_fmt(_clT4, _clT5)}` +
    ` | P4(cycleChildren):${_fmt(_clT5, _clT6)}` +
    ` | P5(iterChildren):${_fmt(_clT6, _clT7)}` +
    ` | P6(scLeaves):${_fmt(_clT7, _clT8)}` +
    ` | collect:${_fmt(_clT8, _clTEnd)}`
  )

  return {
    nodes: [...parentNodes, ...iterContainerNodes, ...directChildrenNodes, ...subCycleContainerNodes, ...subCycleLeafNodes],
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
