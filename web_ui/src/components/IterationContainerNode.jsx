import { memo } from 'react'

const ACCENT_COLORS = {
  test_execution:    { border: 'rgba(16,185,129,0.6)',  bg: 'rgba(16,185,129,0.08)',  pill: '#10b981' },
  fix_cycle:         { border: 'rgba(217,119,6,0.6)',   bg: 'rgba(217,119,6,0.10)',   pill: '#d97706' },
  warning_review:    { border: 'rgba(245,158,11,0.6)',  bg: 'rgba(245,158,11,0.10)',  pill: '#f59e0b' },
  systemic_analysis: { border: 'rgba(139,92,246,0.6)',  bg: 'rgba(139,92,246,0.12)',  pill: '#8b5cf6' },
  systemic_fix:      { border: 'rgba(167,139,250,0.6)', bg: 'rgba(167,139,250,0.12)', pill: '#a78bfa' },
  default:           { border: 'rgba(99,102,241,0.6)',  bg: 'rgba(99,102,241,0.12)',  pill: '#6366f1' },
}

/**
 * Container node for a single review cycle iteration, repair cycle test cycle,
 * or repair sub-cycle (test execution / fix cycle).
 * Renders as a subtle box with a small header pill.
 * Children are stacked vertically inside.
 *
 * Props (via data):
 *   iterationNumber {number}  - 1-based iteration / sub-cycle number
 *   label           {string}  - e.g. "Iteration 1", "Unit Tests", "Fix Cycle 1"
 *   cycleType       {string}  - optional: 'test_execution' | 'fix_cycle' → drives accent color
 */
const IterationContainerNode = ({ data }) => {
  const { label = 'Iteration', iterationNumber, cycleType } = data
  const colors = ACCENT_COLORS[cycleType] || ACCENT_COLORS.default

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        border: `1px solid ${colors.border}`,
        borderRadius: '8px',
        background: colors.bg,
        position: 'relative',
        pointerEvents: 'none', // Let child nodes handle interactions
      }}
    >
      {/* Header pill */}
      <div
        style={{
          position: 'absolute',
          top: -14,
          left: '50%',
          transform: 'translateX(-50%)',
          background: colors.pill,
          color: 'white',
          fontSize: '10px',
          fontWeight: 700,
          padding: '2px 10px',
          borderRadius: '10px',
          whiteSpace: 'nowrap',
          pointerEvents: 'none',
          letterSpacing: '0.3px',
          textTransform: 'uppercase',
        }}
      >
        {label || `Iteration ${iterationNumber}`}
      </div>
    </div>
  )
}

export default memo(IterationContainerNode)
