import { memo } from 'react'
import { GitPullRequest } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

// ── Local helpers ─────────────────────────────────────────────────────────────

const statusDot = color => (
  <div style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
)

// ── Collapsed summary renderer ────────────────────────────────────────────────

function renderCollapsedSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'completed' ? '#10b981' :
    s.status === 'failed'    ? '#ef4444' : '#0ea5e9'

  const statusText = (s.finalStatus ?? s.status).toUpperCase()

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {statusDot(statusColor)}
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
      </div>
      <div style={{ fontSize: 11, color: '#7dd3fc' }}>
        {s.phaseCount} review phase{s.phaseCount !== 1 ? 's' : ''}
      </div>
    </div>
  )
}

// ── Theme ─────────────────────────────────────────────────────────────────────

const PR_REVIEW_THEME = {
  borderColor:           '#0ea5e9',
  bgColor:               'rgba(14,165,233,0.12)',
  cornerColor:           'rgba(14,165,233,0.5)',
  icon:                  GitPullRequest,
  countSuffix:           'phase',
  collapsedLabel:        'PR Review',
  collapsedTextColor:    '#7dd3fc',
  collapsedCountColor:   '#e0f2fe',
  collapsedWidth:        300,
  renderCollapsedSummary,
}

const PRReviewCycleContainerNode = props => <CycleContainerNode {...props} theme={PR_REVIEW_THEME} />

export default memo(PRReviewCycleContainerNode)
