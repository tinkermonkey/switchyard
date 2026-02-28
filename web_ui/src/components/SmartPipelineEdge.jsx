import { useNodes, BaseEdge, SmoothStepEdge } from '@xyflow/react'
import {
  getSmartEdge,
  pathfindingJumpPointNoDiagonal,
} from '@jalez/react-flow-smart-edge'

/**
 * Builds an obstacle node list suitable for getSmartEdge():
 *
 * 1. Converts positions to absolute coordinates.
 *    RF v12 stores child-node positions relative to their parent, but
 *    getSmartEdge() expects everything in the same coordinate space as
 *    the edge endpoints (sourceX/Y, targetX/Y), which are absolute.
 *
 * 2. Populates node.width / node.height.
 *    The library reads node.width/node.height (RF v10/v11 API).
 *    RF v12 stores real DOM dimensions in node.measured.width/height for
 *    leaf nodes, and layout-assigned dimensions in node.style.width/height
 *    for container nodes (which have measured={1,1} as a placeholder).
 *
 * 3. Excludes only COMMON ancestors of source and target.
 *    A container that encloses BOTH endpoints is transit space — the edge
 *    lives inside it, so it must not be an obstacle.
 *    A container that encloses only ONE endpoint (e.g. the iteration container
 *    the source is in but the target is not) stays as an obstacle. The
 *    library's guaranteeWalkablePath() punches a walkable tunnel out of it
 *    in the handle direction, so A* can still start/end inside such a
 *    container and will route around its walls everywhere else.
 */
function buildObstacleNodes(allNodes, sourceId, targetId) {
  const nodeMap = new Map(allNodes.map(n => [n.id, n]))
  const absCache = new Map()

  function absolutePos(node) {
    if (absCache.has(node.id)) return absCache.get(node.id)
    let pos
    if (!node.parentId) {
      pos = { x: node.position.x, y: node.position.y }
    } else {
      const parent = nodeMap.get(node.parentId)
      const parentAbs = parent ? absolutePos(parent) : { x: 0, y: 0 }
      pos = { x: parentAbs.x + node.position.x, y: parentAbs.y + node.position.y }
    }
    absCache.set(node.id, pos)
    return pos
  }

  function ancestorIdSet(nodeId) {
    const ids = new Set()
    let node = nodeMap.get(nodeId)
    while (node?.parentId) {
      ids.add(node.parentId)
      node = nodeMap.get(node.parentId)
    }
    return ids
  }

  // Only exempt containers that are ancestors of BOTH source and target —
  // i.e. containers that fully enclose the entire edge path.
  // Containers enclosing only one endpoint remain obstacles; the library's
  // guaranteeWalkablePath() handles punching the exit/entry tunnel.
  const sourceAncs = ancestorIdSet(sourceId)
  const targetAncs = ancestorIdSet(targetId)
  const exemptIds = new Set([...sourceAncs].filter(id => targetAncs.has(id)))

  return allNodes
    .filter(n => !exemptIds.has(n.id))
    .map(n => {
      // Leaf nodes (pipelineEvent) have real measured dims; container nodes
      // have measured={1,1} placeholder so fall back to style dims.
      const width = n.measured?.width > 1
        ? n.measured.width
        : (n.style?.width || 200)
      const height = n.measured?.height > 1
        ? n.measured.height
        : (n.style?.height || 80)
      return { ...n, position: absolutePos(n), width, height }
    })
}

const CORNER_RADIUS = 5

/**
 * Custom draw function that:
 * 1. Snaps the first/last interior waypoints to the source/target handle axis
 *    so the initial segment is perfectly straight (no kink from grid rounding).
 * 2. Removes collinear intermediate points (every grid cell from JumpPoint).
 * 3. Removes near-duplicate points (< 0.5 px apart).
 * 4. Rounds each turn with a quadratic bezier at CORNER_RADIUS px.
 *
 * The library calls drawEdge(source, target, path) where:
 *   source = { x, y, position }   (the handle)
 *   target = { x, y, position }
 *   path   = [[x,y], ...]         (interior waypoints from the pathfinder)
 */
function drawSmartEdge(source, target, path) {
  if (!path || path.length === 0) {
    return `M ${source.x},${source.y} L ${target.x},${target.y}`
  }

  // Build full point list: handle → interior waypoints → handle
  let pts = [[source.x, source.y], ...path, [target.x, target.y]]

  // Forward propagation from source: each interior point inherits its
  // "stationary" coordinate from the previous real point. Because the
  // pathfinder grid-rounds every cell (e.g. handle at x=354.77 snaps
  // to x=360), the first—and every subsequent—grid cell can be offset.
  // Propagating from the actual handle position threads the real
  // coordinates through the entire source-side of the path.
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = pts[i - 1]
    const [cx, cy] = pts[i]
    const dx = Math.abs(cx - px), dy = Math.abs(cy - py)
    pts[i] = dy >= dx ? [px, cy] : [cx, py]
  }

  // Backward propagation from target: same logic in reverse, threading
  // the real target handle position through the target-approaching segments.
  // Because each pass only overwrites the "stationary" axis, the two passes
  // are compatible: forward fixes source side, backward fixes target side,
  // and they agree at every interior turn point.
  for (let i = pts.length - 2; i >= 1; i--) {
    const [cx, cy] = pts[i]
    const [nx, ny] = pts[i + 1]
    const dx = Math.abs(nx - cx), dy = Math.abs(ny - cy)
    pts[i] = dy >= dx ? [nx, cy] : [cx, ny]
  }

  // Remove collinear intermediate points. After propagation, all adjacent
  // same-direction steps (the raw grid cells from JumpPointNoDiagonal)
  // share the same x or y value and collapse to just the turn points.
  const clean = [pts[0]]
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = clean[clean.length - 1]
    const [cx, cy] = pts[i]
    const [nx, ny] = pts[i + 1]
    if (!(px === cx && cx === nx) && !(py === cy && cy === ny)) clean.push(pts[i])
  }
  clean.push(pts[pts.length - 1])

  // Remove near-duplicate points (< 0.5 px apart)
  pts = clean.filter((p, i) =>
    i === 0 ||
    Math.abs(p[0] - clean[i - 1][0]) > 0.5 ||
    Math.abs(p[1] - clean[i - 1][1]) > 0.5
  )

  if (pts.length < 2) return `M ${source.x},${source.y} L ${target.x},${target.y}`

  // Build SVG path with rounded corners at each turn
  let d = `M ${pts[0][0]},${pts[0][1]}`
  for (let i = 1; i < pts.length; i++) {
    const [x, y] = pts[i]
    const [px, py] = pts[i - 1]

    if (i === pts.length - 1) {
      d += ` L ${x},${y}`
      continue
    }

    const [nx, ny] = pts[i + 1]
    const seg = Math.hypot(x - px, y - py)
    const nxt = Math.hypot(nx - x, ny - y)
    const r = Math.min(CORNER_RADIUS, seg / 2, nxt / 2)

    if (r < 1) {
      d += ` L ${x},${y}`
    } else {
      const idx = (x - px) / seg, idy = (y - py) / seg
      const odx = (nx - x) / nxt, ody = (ny - y) / nxt
      d += ` L ${x - idx * r},${y - idy * r}`
      d += ` Q ${x},${y} ${x + odx * r},${y + ody * r}`
    }
  }
  return d
}

export default function SmartPipelineEdge(props) {
  const { sourcePosition, targetPosition, sourceX, sourceY, targetX, targetY, source, target } = props
  const allNodes = useNodes()
  const obstacleNodes = buildObstacleNodes(allNodes, source, target)

  const result = getSmartEdge({
    sourcePosition,
    targetPosition,
    sourceX,
    sourceY,
    targetX,
    targetY,
    nodes: obstacleNodes,
    options: {
      nodePadding: 12,
      gridRatio: 10,
      drawEdge: drawSmartEdge,
      generatePath: pathfindingJumpPointNoDiagonal,
    },
  })

  if (result === null) {
    return <SmoothStepEdge {...props} />
  }

  return <BaseEdge path={result.svgPathString} {...props} />
}
