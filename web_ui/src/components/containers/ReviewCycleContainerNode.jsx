import { memo } from 'react'
import { RotateCcw } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

// ── Local helpers ─────────────────────────────────────────────────────────────

const formatAgent = str =>
  str ? str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()) : ''

const statusDot = color => (
  <div style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
)

const fmtDur = s => {
  if (s == null) return '—'
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60), sec = s % 60
  return sec > 0 ? `${m}m ${sec}s` : `${m}m`
}

// ── Collapsed summary renderer ────────────────────────────────────────────────

function renderCollapsedSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'approved'  ? '#10b981' :
    s.status === 'rejected'  ? '#ef4444' :
    s.status === 'escalated' ? '#f59e0b' : '#9333ea'

  const BAR_MAX_W = 180
  const totalDur = s.durationSeconds
  const iterations = s.iterations ?? []

  // compute max iteration duration for proportional bars
  const maxIterDur = iterations.reduce((mx, it) => Math.max(mx, it.durationSeconds ?? 0), 0)

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status + total duration */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {statusDot(statusColor)}
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>
          {s.status.toUpperCase()}
        </span>
        {totalDur != null && (
          <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>
            {fmtDur(totalDur)}
          </span>
        )}
      </div>

      {/* Maker / Reviewer */}
      {s.makerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: '#6b7280', minWidth: 52 }}>Maker</span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.makerAgent)}</span>
        </div>
      )}
      {s.reviewerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: '#6b7280', minWidth: 52 }}>Reviewer</span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.reviewerAgent)}</span>
        </div>
      )}

      {/* Iteration timeline */}
      {iterations.length > 0 && (
        <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 3 }}>
          <span style={{ fontSize: 9, color: '#6b7280', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            Iteration timeline
          </span>
          {iterations.map((iter, idx) => {
            const isFirst = idx === 0
            const barColor = isFirst ? '#9333ea' : '#f59e0b'
            const isRunning = iter.durationSeconds == null && s.status === 'running' && idx === iterations.length - 1
            const barW = iter.durationSeconds != null && maxIterDur > 0
              ? Math.max(4, Math.round((iter.durationSeconds / maxIterDur) * BAR_MAX_W))
              : isRunning ? 40 : 4

            return (
              <div key={iter.number} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 9, color: '#6b7280', minWidth: 10, textAlign: 'right' }}>
                  {iter.number}
                </span>
                <div
                  style={{
                    height: 6,
                    width: barW,
                    borderRadius: 3,
                    background: barColor,
                    opacity: isRunning ? undefined : 0.9,
                    animation: isRunning ? 'pulse 1.5s ease-in-out infinite' : undefined,
                  }}
                />
                <span style={{ fontSize: 9, color: '#9ca3af' }}>
                  {fmtDur(iter.durationSeconds)}
                </span>
              </div>
            )
          })}
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
  collapsedWidth:        320,
  renderCollapsedSummary,
}

const ReviewCycleContainerNode = props => <CycleContainerNode {...props} theme={REVIEW_CYCLE_THEME} />

export default memo(ReviewCycleContainerNode)
