import { memo } from 'react'
import { RotateCcw } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

// ── Local helpers ─────────────────────────────────────────────────────────────

const formatAgent = str =>
  str ? str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()) : ''

const statusDot = color => (
  <div style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
)

// ── Collapsed summary renderer ────────────────────────────────────────────────

function renderCollapsedSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'approved'  ? '#10b981' :
    s.status === 'rejected'  ? '#ef4444' :
    s.status === 'escalated' ? '#f59e0b' : '#9333ea'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {/* Status + iteration count */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {statusDot(statusColor)}
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>
          {s.status.toUpperCase()}
        </span>
        <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>
          {s.totalIterations}{s.maxIterations ? ` / ${s.maxIterations}` : ''} iter{s.totalIterations !== 1 ? 's' : ''}
        </span>
      </div>

      {s.makerAgent && (
        <div style={{ fontSize: 10 }}>
          <span style={{ color: '#6b7280' }}>Maker: </span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.makerAgent)}</span>
        </div>
      )}
      {s.reviewerAgent && (
        <div style={{ fontSize: 10 }}>
          <span style={{ color: '#6b7280' }}>Reviewer: </span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.reviewerAgent)}</span>
        </div>
      )}
    </div>
  )
}

// ── Theme ─────────────────────────────────────────────────────────────────────

const REVIEW_CYCLE_THEME = {
  borderColor:           '#9333ea',
  bgColor:               'rgba(147, 51, 234, 0.14)',
  cornerColor:           'rgba(147,51,234,0.6)',
  icon:                  RotateCcw,
  countSuffix:           'iteration',
  collapsedLabel:        'Review Cycle',
  collapsedTextColor:    '#c4b5fd',
  collapsedCountColor:   '#e9d5ff',
  collapsedWidth:        300,
  renderCollapsedSummary,
}

const ReviewCycleContainerNode = props => <CycleContainerNode {...props} theme={REVIEW_CYCLE_THEME} />

export default memo(ReviewCycleContainerNode)
