import { Handle, Position } from '@xyflow/react'

/**
 * Standard four-sided connection handles for all pipeline graph nodes.
 *
 * Named handle IDs create a shared contract between node components and edge
 * builders — edges reference handles by name rather than relying on React Flow's
 * default selection, which is ambiguous when multiple handles of the same type exist.
 *
 *   top / bottom  — vertical sequential flow  (PipelineFlowGraph)
 *   left / right  — horizontal backbone flow  (PromptsFlowGraph)
 *
 * Every node that participates in either graph renders this component so the
 * contract is defined in one place. Handles are invisible; styling is left
 * entirely to the edge.
 */
export default function NodeHandles() {
  return (
    <>
      <Handle id="top"    type="target" position={Position.Top}    style={{ opacity: 0 }} />
      <Handle id="left"   type="target" position={Position.Left}   style={{ opacity: 0 }} />
      <Handle id="right"  type="source" position={Position.Right}  style={{ opacity: 0 }} />
      <Handle id="bottom" type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </>
  )
}
