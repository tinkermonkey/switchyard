import { Handle, Position } from '@xyflow/react'
import { ChevronDown, ChevronRight } from 'lucide-react'

/**
 * Base component for top-level expandable cycle containers.
 * Renders a dashed bounding box with a coloured header bar, collapse toggle,
 * corner decorations, and an optional collapsed summary view.
 *
 * Not registered as a node type directly — used via themed wrappers.
 *
 * Props:
 *   data.cycleId          {string}
 *   data.label            {string}   - header title
 *   data.iterationCount   {number}
 *   data.isCollapsed      {boolean}
 *   data.onToggleCollapse {function}
 *
 *   theme.borderColor       {string}   - e.g. '#9333ea'
 *   theme.bgColor           {string}   - e.g. 'rgba(147,51,234,0.14)'
 *   theme.cornerColor       {string}   - semi-transparent version for corner decorations
 *   theme.icon              {Component}- lucide-react icon component
 *   theme.countSuffix       {string}   - e.g. 'iteration' or 'test cycle'
 *   theme.collapsedLabel    {string}   - label shown in collapsed card
 *   theme.collapsedTextColor  {string}
 *   theme.collapsedCountColor {string}
 */
export function CycleContainerNode({ data, theme }) {
  const {
    cycleId,
    label,
    iterationCount = 0,
    isCollapsed = false,
    onToggleCollapse,
  } = data

  const {
    borderColor,
    bgColor,
    cornerColor,
    icon: Icon,
    countSuffix = 'cycle',
    collapsedLabel,
    collapsedTextColor,
    collapsedCountColor,
  } = theme

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
        borderColor,
        borderRadius: '12px',
        background: bgColor,
        position: 'relative',
        transition: 'all 0.3s ease',
        pointerEvents: 'all',
      }}
    >
      <Handle
        type="target"
        position={Position.Top}
        style={{ background: borderColor, width: 10, height: 10, border: '2px solid white', opacity: isCollapsed ? 1 : 0 }}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        style={{ background: borderColor, width: 10, height: 10, border: '2px solid white', opacity: isCollapsed ? 1 : 0 }}
      />

      {/* Header bar */}
      <div
        onClick={handleToggle}
        onMouseEnter={e => (e.currentTarget.style.background = borderColor)}
        onMouseLeave={e => (e.currentTarget.style.background = `${borderColor}e6`)}
        style={{
          position: 'absolute',
          top: 0, left: 0, right: 0,
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px', height: 30,
          background: `${borderColor}e6`,
          color: 'white', borderRadius: '10px 10px 0 0',
          fontSize: 13, fontWeight: 600,
          cursor: 'pointer', userSelect: 'none',
          transition: 'background 0.2s ease',
        }}
      >
        <Icon className="w-4 h-4" />
        <span style={{ flex: 1 }}>{label}</span>
        <span style={{ fontSize: 11, opacity: 0.9 }}>
          {iterationCount} {countSuffix}{iterationCount !== 1 ? 's' : ''}
        </span>
        {isCollapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
      </div>

      {/* Collapsed summary */}
      {isCollapsed && (
        <div
          style={{
            padding: '20px',
            display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center',
            height: '100%', gap: 8,
          }}
        >
          <div style={{ fontSize: 11, color: collapsedTextColor, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            {collapsedLabel}
          </div>
          <div style={{ fontSize: 24, fontWeight: 700, color: collapsedCountColor }}>
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
            { top: 8,    left: 8,  borderTop: true,    borderLeft: true,  borderRadius: '4px 0 0 0' },
            { top: 8,    right: 8, borderTop: true,    borderRight: true, borderRadius: '0 4px 0 0' },
            { bottom: 8, left: 8,  borderBottom: true, borderLeft: true,  borderRadius: '0 0 0 4px' },
            { bottom: 8, right: 8, borderBottom: true, borderRight: true, borderRadius: '0 0 4px 0' },
          ].map((corner, i) => (
            <div
              key={i}
              style={{
                position: 'absolute', width: 16, height: 16, ...corner,
                ...(corner.borderTop    ? { borderTop:    `2px solid ${cornerColor}` } : {}),
                ...(corner.borderBottom ? { borderBottom: `2px solid ${cornerColor}` } : {}),
                ...(corner.borderLeft   ? { borderLeft:   `2px solid ${cornerColor}` } : {}),
                ...(corner.borderRight  ? { borderRight:  `2px solid ${cornerColor}` } : {}),
              }}
            />
          ))}
        </>
      )}
    </div>
  )
}
