import { memo } from 'react'
import { MessageSquare } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

// ── Local helpers ─────────────────────────────────────────────────────────────

const statusDot = color => (
  <div style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
)

const fmtDur = s => {
  if (s == null) return ''
  const m = Math.floor(s / 60)
  const sec = s % 60
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`
}

// ── Collapsed summary renderer ────────────────────────────────────────────────

function renderCollapsedSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor = s.status === 'paused' ? '#9ca3af' : '#ec4899'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {statusDot(statusColor)}
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>
          {s.status.toUpperCase()}
        </span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>
            {fmtDur(s.durationSeconds)}
          </span>
        )}
      </div>
      <div style={{ fontSize: 11, color: '#f9a8d4' }}>
        {s.exchangeCount} exchange{s.exchangeCount !== 1 ? 's' : ''}
      </div>
    </div>
  )
}

// ── Theme ─────────────────────────────────────────────────────────────────────

const CONV_LOOP_THEME = {
  borderColor:           '#ec4899',
  bgColor:               'rgba(236,72,153,0.12)',
  cornerColor:           'rgba(236,72,153,0.5)',
  icon:                  MessageSquare,
  countSuffix:           'question',
  collapsedLabel:        'Conversation',
  collapsedTextColor:    '#f9a8d4',
  collapsedCountColor:   '#fce7f3',
  collapsedWidth:        280,
  renderCollapsedSummary,
}

const ConversationalLoopContainerNode = props => <CycleContainerNode {...props} theme={CONV_LOOP_THEME} />

export default memo(ConversationalLoopContainerNode)
