import { memo } from 'react'
import { RotateCcw, Wrench, GitPullRequest, Box } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

/**
 * Themed wrappers for Level-2 iteration/phase containers inside each top-level cycle.
 * Uses CycleContainerNode as base. Collapsed by default; onToggleCollapse injected by caller.
 * Theme is selected by data.cycleType, injected by buildFlowchart.js.
 *
 * cycleType values: 'review' | 'repair' | 'pr_review'
 */

// ── Local helpers ─────────────────────────────────────────────────────────────

const formatAgent = str =>
  str ? str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()) : ''

const fmtDur = s => {
  if (s == null) return ''
  const m = Math.floor(s / 60)
  const sec = s % 60
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`
}

// ── Collapsed summary renderers ───────────────────────────────────────────────

function renderRepairSummary(data) {
  const s = data.summary
  if (!s) return null

  const passColor = s.passed ? '#10b981' : s.passed != null ? '#ef4444' : '#9ca3af'
  const passText = s.passed ? '✓ Passed' : s.passed != null ? '✗ Failed' : '…'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {/* Status row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
        <span style={{ fontWeight: 700, color: passColor }}>{passText}</span>
        {s.filesFixed != null && s.filesFixed > 0 && (
          <span style={{ color: '#9ca3af' }}>{s.filesFixed} files fixed</span>
        )}
        {s.durationSeconds != null && (
          <span style={{ color: '#9ca3af', marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {/* Test result counts */}
      {s.testResultRow && (s.testResultRow.passedCount != null || s.testResultRow.failedCount != null) && (
        <div style={{ fontSize: 10, color: '#6b7280' }}>
          {'Tests: '}
          {s.testResultRow.passedCount != null && (
            <span style={{ color: '#10b981' }}>{s.testResultRow.passedCount} pass</span>
          )}
          {s.testResultRow.passedCount != null && s.testResultRow.failedCount != null && ' / '}
          {s.testResultRow.failedCount != null && (
            <span style={{ color: '#ef4444' }}>{s.testResultRow.failedCount} fail</span>
          )}
          {s.testResultRow.warningsCount != null && (
            <>{' / '}<span style={{ color: '#f59e0b' }}>{s.testResultRow.warningsCount} warn</span></>
          )}
        </div>
      )}

      {/* Warnings reviewed */}
      {s.warningsReviewed != null && s.warningsReviewed > 0 && (
        <div style={{ fontSize: 10, color: '#f59e0b' }}>{s.warningsReviewed} warnings reviewed</div>
      )}

      {/* Systemic fix applied */}
      {s.hadSystemicFix && (
        <div style={{ fontSize: 10, color: '#a78bfa' }}>🔍 Systemic fix applied</div>
      )}
    </div>
  )
}

function renderReviewSummary(data) {
  const s = data.summary
  if (!s || (!s.makerAgent && !s.reviewerAgent)) return null

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
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

function renderPRReviewSummary(data) {
  const s = data.summary
  if (!s) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ fontSize: 11, color: '#7dd3fc' }}>
        {s.eventCount} event{s.eventCount !== 1 ? 's' : ''}
      </div>
    </div>
  )
}

// ── Theme map ─────────────────────────────────────────────────────────────────

const ITERATION_THEME_MAP = {
  review: {
    borderColor:             '#9333ea',
    borderStyle:             'solid',
    bgColor:                 'rgba(147,51,234,0.06)',
    cornerColor:             'rgba(147,51,234,0.35)',
    icon:                    RotateCcw,
    countSuffix:             'event',
    collapsedLabel:          'Iteration',
    collapsedTextColor:      '#c4b5fd',
    collapsedCountColor:     '#e9d5ff',
    collapsedWidth:          280,
    renderCollapsedSummary:  renderReviewSummary,
  },
  repair: {
    borderColor:             '#d97706',
    borderStyle:             'solid',
    bgColor:                 'rgba(217,119,6,0.06)',
    cornerColor:             'rgba(217,119,6,0.35)',
    icon:                    Wrench,
    countSuffix:             'sub-cycle',
    collapsedLabel:          'Test Cycle',
    collapsedTextColor:      '#fcd34d',
    collapsedCountColor:     '#fef3c7',
    collapsedWidth:          300,
    renderCollapsedSummary:  renderRepairSummary,
  },
  pr_review: {
    borderColor:             '#0ea5e9',
    borderStyle:             'solid',
    bgColor:                 'rgba(14,165,233,0.06)',
    cornerColor:             'rgba(14,165,233,0.35)',
    icon:                    GitPullRequest,
    countSuffix:             'event',
    collapsedLabel:          'Phase',
    collapsedTextColor:      '#7dd3fc',
    collapsedCountColor:     '#e0f2fe',
    collapsedWidth:          280,
    renderCollapsedSummary:  renderPRReviewSummary,
  },
  default: {
    borderColor:             '#6366f1',
    borderStyle:             'solid',
    bgColor:                 'rgba(99,102,241,0.06)',
    cornerColor:             'rgba(99,102,241,0.35)',
    icon:                    Box,
    countSuffix:             'event',
    collapsedLabel:          'Iteration',
    collapsedTextColor:      '#a5b4fc',
    collapsedCountColor:     '#e0e7ff',
  },
}

const IterationContainerNode = ({ data, ...props }) => {
  const theme = ITERATION_THEME_MAP[data.cycleType] || ITERATION_THEME_MAP.default
  return <CycleContainerNode data={data} {...props} theme={theme} />
}

export default memo(IterationContainerNode)
