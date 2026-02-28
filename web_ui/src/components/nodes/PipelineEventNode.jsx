import { Handle, Position } from '@xyflow/react'

/**
 * Root base component for all pipeline event nodes.
 *
 * Provides shared structure: ReactFlow source/target handles, the candy-stripe
 * active-agent animation, and the label + metadata layout. All visual
 * customisation (colours, icon) is injected by the composition chain above.
 *
 * This component is also registered as the 'pipelineEvent' fallback in the
 * nodeTypes map, so unknown event types degrade gracefully with a neutral style.
 *
 * Composition pattern — each level wraps the one above:
 *   ReactFlow → LeafNode({ data })
 *             → IntermediaryNode({ data, nodeStyle?, icon? })
 *             → … → PipelineEventNode({ data, nodeStyle, icon })
 *
 * Leaves only accept { data } from ReactFlow. They construct their own nodeStyle
 * and icon and pass them down. Intermediaries merge their DEFAULT_STYLE with any
 * incoming nodeStyle override, allowing sub-families to override parent colours.
 *
 * Props:
 *   data        - ReactFlow node data (label, metadata, isActive)
 *   nodeStyle   - Style object merged on top of baseStyle (background, borderColor, color, …)
 *   icon        - ReactNode rendered to the left of the label
 */
export default function PipelineEventNode({ data, nodeStyle, icon }) {
  if (!data) return null
  const { label, metadata, isActive } = data

  const style = {
    padding: '12px 16px',
    borderRadius: '8px',
    border: '2px solid',
    minWidth: '200px',
    maxWidth: '300px',
    background: '#374151',
    borderColor: '#4b5563',
    color: '#fff',
    boxShadow: isActive ? '0 0 10px rgba(88, 166, 255, 0.5)' : '0 2px 4px rgba(0,0,0,0.1)',
    ...nodeStyle,
  }

  return (
    <div style={style} className="relative">
      {isActive && (
        <div
          className="absolute top-0 left-0 right-0 h-1 rounded-t-md overflow-hidden"
          style={{
            backgroundImage:
              'linear-gradient(45deg, rgba(255,255,255,.2) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.2) 50%, rgba(255,255,255,.2) 75%, transparent 75%, transparent)',
            backgroundSize: '1rem 1rem',
            animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite, stripes 1s linear infinite',
          }}
        />
      )}

      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />

      <div className="flex items-start gap-2">
        {icon && <div className="mt-0.5 shrink-0">{icon}</div>}
        <div className="flex-1 min-w-0">
          <div className="font-semibold text-sm">{label}</div>
          {metadata && <div className="text-xs mt-1 opacity-90">{metadata}</div>}
        </div>
      </div>

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}
