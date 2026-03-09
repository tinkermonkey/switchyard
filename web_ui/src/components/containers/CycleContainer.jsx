import { Handle, Position, NodeResizer } from '@xyflow/react'
import { ChevronDown, ChevronRight } from 'lucide-react'

/**
 * Base component for expandable cycle containers at any nesting level.
 * Renders a dashed (or solid) bounding box with a coloured header bar, optional
 * collapse toggle, corner decorations, and an optional collapsed summary view.
 *
 * Not registered as a node type directly — used via CycleContainerNode (unified dispatcher).
 *
 * Props:
 *   data.cycleId          {string}   - used as toggle callback arg; optional for sub-containers
 *   data.label            {string}   - header title
 *   data.iterationCount   {number}   - shown in header when > 0
 *   data.isCollapsed      {boolean}  - collapse state (sub-containers: always false)
 *   data.onToggleCollapse {function} - if null/undefined, collapse toggle is hidden
 *   data.isResizable      {boolean}  - optional: renders NodeResizer when true and not collapsed
 *
 *   theme.borderColor         {string}    - e.g. '#9333ea'
 *   theme.borderStyle         {string}    - 'solid' (default) or other CSS border-style
 *   theme.bgColor             {string}    - e.g. 'rgba(147,51,234,0.14)'
 *   theme.cornerColor         {string}    - semi-transparent version for corner decorations
 *   theme.icon                {Component} - lucide-react icon component
 *   theme.collapsedCountColor {string}    - color for the iteration count in the fallback collapsed body
 */
export function CycleContainer({ data, theme }) {
  const {
    cycleId,
    label,
    iterationCount = 0,
    isCollapsed = false,
    onToggleCollapse,
    isResizable = false,
  } = data

  const {
    borderColor,
    borderStyle = 'solid',
    bgColor,
    cornerColor,
    icon: Icon,
    collapsedCountColor,
    collapsedWidth = 280,
    renderCollapsedSummary,
  } = theme

  const isToggleable = !!onToggleCollapse

  const handleToggle = e => {
    e.stopPropagation()
    if (onToggleCollapse) onToggleCollapse(cycleId)
  }

  // ── Collapsed rendering ──────────────────────────────────────────────────
  // No explicit height — React Flow measures the rendered content via ResizeObserver.
  // The layout engine uses measured.height (not style.height) for handle positioning.
  if (isCollapsed) {
    const headerStyle = {
      display: 'flex', alignItems: 'center', gap: 6,
      padding: '0 10px', height: 28,
      background: `${borderColor}e6`,
      color: 'white', borderRadius: '10px 10px 0 0',
      fontSize: 11, fontWeight: 600,
      cursor: isToggleable ? 'pointer' : 'default',
      userSelect: 'none',
    }
    return (
      <div
        style={{
          width: collapsedWidth,
          display: 'flex',
          flexDirection: 'column',
          border: `2px ${borderStyle} ${borderColor}`,
          borderRadius: '12px',
          background: bgColor,
          position: 'relative',
          pointerEvents: 'all',
        }}
      >
        <Handle
          type="target"
          position={Position.Top}
          style={{ background: borderColor, width: 10, height: 10, border: '2px solid white', zIndex: 10 }}
        />
        <Handle
          type="source"
          position={Position.Bottom}
          style={{ background: borderColor, width: 10, height: 10, border: '2px solid white', zIndex: 10 }}
        />
        <div
          onClick={isToggleable ? handleToggle : undefined}
          onMouseEnter={isToggleable ? e => (e.currentTarget.style.background = borderColor) : undefined}
          onMouseLeave={isToggleable ? e => (e.currentTarget.style.background = `${borderColor}e6`) : undefined}
          style={headerStyle}
        >
          <Icon style={{ width: 13, height: 13 }} />
          <span>{label}</span>
          {isToggleable && <ChevronRight style={{ width: 13, height: 13 }} />}
        </div>
        {(() => {
          const customBody = renderCollapsedSummary ? renderCollapsedSummary(data) : null
          if (customBody != null) return customBody
          return (
            <div
              style={{
                display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center',
                padding: '12px 20px', gap: 8,
              }}
            >
              <div style={{ fontSize: 24, fontWeight: 700, color: collapsedCountColor }}>
                {iterationCount}×
              </div>
              <div style={{ fontSize: 11, color: '#9ca3af', textAlign: 'center' }}>
                Click to expand
              </div>
            </div>
          )
        })()}
      </div>
    )
  }

  // ── Expanded rendering ────────────────────────────────────────────────────
  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        border: `2px ${borderStyle} ${borderColor}`,
        borderRadius: '12px',
        background: bgColor,
        position: 'relative',
        transition: 'all 0.3s ease',
        pointerEvents: 'all',
      }}
    >
      <NodeResizer
        isVisible={isResizable}
        minWidth={220}
        minHeight={80}
        handleStyle={{ background: '#6b7280', border: '1px solid white', borderRadius: '2px' }}
        lineStyle={{ borderColor: '#6b728060' }}
      />

      <Handle
        type="target"
        position={Position.Top}
        style={{ background: borderColor, width: 10, height: 10, border: '2px solid white', opacity: 0 }}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        style={{ background: borderColor, width: 10, height: 10, border: '2px solid white', opacity: 0 }}
      />

      {/* Header bar */}
      <div
        onClick={isToggleable ? handleToggle : undefined}
        onMouseEnter={isToggleable ? e => (e.currentTarget.style.background = borderColor) : undefined}
        onMouseLeave={isToggleable ? e => (e.currentTarget.style.background = `${borderColor}e6`) : undefined}
        style={{
          position: 'absolute',
          top: 0, left: 0, right: 0,
          display: 'flex', alignItems: 'center', gap: 6,
          padding: '0 10px', height: 28,
          background: `${borderColor}e6`,
          color: 'white', borderRadius: '10px 10px 0 0',
          fontSize: 11, fontWeight: 600,
          cursor: isToggleable ? 'pointer' : 'default',
          userSelect: 'none',
          transition: 'background 0.2s ease',
        }}
      >
        <Icon style={{ width: 13, height: 13 }} />
        <span>{label}</span>
        {isToggleable && <ChevronDown style={{ width: 13, height: 13 }} />}
      </div>

      {/* Corner decorations */}
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
    </div>
  )
}
