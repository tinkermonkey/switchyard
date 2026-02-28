import { useNodes, BaseEdge, SmoothStepEdge } from '@xyflow/react'
import {
  getSmartEdge,
  svgDrawStraightLinePath,
  pathfindingAStarNoDiagonal,
} from '@jalez/react-flow-smart-edge'

// iterationContainers sit fully inside cycle containers — no need to register
// them as separate obstacles; the cycle container already covers their area.
const SKIP_TYPES = new Set(['iterationContainer'])

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
 */
function buildObstacleNodes(allNodes) {
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

  return allNodes
    .filter(n => !SKIP_TYPES.has(n.type))
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

export default function SmartPipelineEdge(props) {
  const { sourcePosition, targetPosition, sourceX, sourceY, targetX, targetY } = props
  const allNodes = useNodes()
  const obstacleNodes = buildObstacleNodes(allNodes)

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
      drawEdge: svgDrawStraightLinePath,
      generatePath: pathfindingAStarNoDiagonal,
    },
  })

  if (result === null) {
    return <SmoothStepEdge {...props} />
  }

  return <BaseEdge path={result.svgPathString} {...props} />
}
