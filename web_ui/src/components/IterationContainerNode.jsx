import { memo } from 'react'

/**
 * Container node for a single review cycle iteration or repair cycle test cycle.
 * Renders as a subtle indigo box with a small header pill.
 * Children are stacked vertically inside.
 *
 * Props (via data):
 *   iterationNumber {number}  - 1-based iteration / test-cycle number
 *   label           {string}  - e.g. "Iteration 1" or "Test Cycle 1"
 *   eventCount      {number}  - number of child events (for reference)
 */
const IterationContainerNode = ({ data }) => {
  const { label = 'Iteration', iterationNumber } = data

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        border: '1px solid rgba(99, 102, 241, 0.6)',
        borderRadius: '8px',
        background: 'rgba(99, 102, 241, 0.12)',
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
          background: '#6366f1',
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
