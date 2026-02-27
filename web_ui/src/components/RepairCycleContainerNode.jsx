import { memo } from 'react'
import { Handle, Position } from '@xyflow/react'
import { ChevronDown, ChevronRight, Wrench } from 'lucide-react'

/**
 * Container node for a repair cycle.
 * Renders as an amber/red dashed bounding box with a header bar.
 * Contains test cycle iteration containers arranged horizontally.
 *
 * Props (via data):
 *   cycleId         {string}
 *   label           {string}
 *   iterationCount  {number}  - number of test cycles
 *   isCollapsed     {boolean}
 *   onToggleCollapse {function}
 *   startTime       {string}
 *   endTime         {string}
 */
const BORDER_COLOR = '#d97706'
const BG_COLOR = 'rgba(217, 119, 6, 0.14)'

const RepairCycleContainerNode = ({ data }) => {
  const {
    cycleId,
    label = 'Repair Cycle',
    iterationCount = 0,
    isCollapsed = false,
    onToggleCollapse,
  } = data

  const handleToggle = e => {
    e.stopPropagation()
    if (onToggleCollapse) onToggleCollapse(cycleId)
  }

  return (
    <div
      style={{
        width: isCollapsed ? 280 : '100%',
        height: isCollapsed ? 100 : '100%',
        border: '2px dashed',
        borderColor: BORDER_COLOR,
        borderRadius: '12px',
        background: BG_COLOR,
        position: 'relative',
        transition: 'all 0.3s ease',
        pointerEvents: 'all',
      }}
    >
      {/* Handles — always present (invisible when expanded) */}
      <Handle
        type="target"
        position={Position.Top}
        style={{
          background: BORDER_COLOR,
          width: 10,
          height: 10,
          border: '2px solid white',
          opacity: isCollapsed ? 1 : 0,
        }}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        style={{
          background: BORDER_COLOR,
          width: 10,
          height: 10,
          border: '2px solid white',
          opacity: isCollapsed ? 1 : 0,
        }}
      />

      {/* Header bar — sits at the top inside the node boundary */}
      <div
        onClick={handleToggle}
        onMouseEnter={e => (e.currentTarget.style.background = BORDER_COLOR)}
        onMouseLeave={e => (e.currentTarget.style.background = `${BORDER_COLOR}e6`)}
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 12px',
          height: 30,
          background: `${BORDER_COLOR}e6`,
          color: 'white',
          borderRadius: '10px 10px 0 0',
          fontSize: 13,
          fontWeight: 600,
          cursor: 'pointer',
          userSelect: 'none',
          transition: 'background 0.2s ease',
        }}
      >
        <Wrench className="w-4 h-4" />
        <span style={{ flex: 1 }}>{label}</span>
        <span style={{ fontSize: 11, opacity: 0.9 }}>
          {iterationCount} test cycle{iterationCount !== 1 ? 's' : ''}
        </span>
        {isCollapsed ? (
          <ChevronRight className="w-4 h-4" />
        ) : (
          <ChevronDown className="w-4 h-4" />
        )}
      </div>

      {/* Collapsed content */}
      {isCollapsed && (
        <div
          style={{
            padding: '20px',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            gap: 8,
          }}
        >
          <div
            style={{
              fontSize: 11,
              color: '#fcd34d',
              fontWeight: 600,
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
            }}
          >
            Repair Cycle
          </div>
          <div style={{ fontSize: 24, fontWeight: 700, color: '#fef3c7' }}>
            {iterationCount}×
          </div>
          <div style={{ fontSize: 11, color: '#9ca3af', textAlign: 'center' }}>
            Click to expand
          </div>
        </div>
      )}

      {/* Corner decorations (expanded only) */}
      {!isCollapsed && (
        <>
          {[
            { top: 8, left: 8, borderTop: true, borderLeft: true, borderRadius: '4px 0 0 0' },
            { top: 8, right: 8, borderTop: true, borderRight: true, borderRadius: '0 4px 0 0' },
            { bottom: 8, left: 8, borderBottom: true, borderLeft: true, borderRadius: '0 0 0 4px' },
            { bottom: 8, right: 8, borderBottom: true, borderRight: true, borderRadius: '0 0 4px 0' },
          ].map((corner, i) => (
            <div
              key={i}
              style={{
                position: 'absolute',
                width: 16,
                height: 16,
                ...corner,
                ...(corner.borderTop ? { borderTop: '2px solid rgba(217,119,6,0.6)' } : {}),
                ...(corner.borderBottom ? { borderBottom: '2px solid rgba(217,119,6,0.6)' } : {}),
                ...(corner.borderLeft ? { borderLeft: '2px solid rgba(217,119,6,0.6)' } : {}),
                ...(corner.borderRight ? { borderRight: '2px solid rgba(217,119,6,0.6)' } : {}),
              }}
            />
          ))}
        </>
      )}
    </div>
  )
}

export default memo(RepairCycleContainerNode)
