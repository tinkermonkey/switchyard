import { memo } from 'react'
import { Wrench, FlaskConical, AlertTriangle, Search, Box } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

/**
 * Themed wrapper for Level-3 sub-cycle containers inside repair cycle test cycles.
 * Uses CycleContainerNode as base. Collapsed by default; onToggleCollapse injected by caller.
 * Theme is selected by data.cycleType, set by buildFlowchart.js via eventProcessing.
 *
 * cycleType values: 'test_execution' | 'fix_cycle' | 'warning_review' | 'systemic_analysis' | 'systemic_fix'
 */

// ── Collapsed summary renderers ───────────────────────────────────────────────

function renderTestExecutionSummary(data) {
  const s = data.summary
  if (!s) return null
  if (s.testPassedCount == null && s.testFailedCount == null) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ display: 'flex', gap: 8, fontSize: 11 }}>
        {s.testPassedCount != null && (
          <span style={{ color: '#10b981' }}>✓ {s.testPassedCount} pass</span>
        )}
        {s.testFailedCount != null && (
          <span style={{ color: '#ef4444' }}>✗ {s.testFailedCount} fail</span>
        )}
      </div>
    </div>
  )
}

function renderFixCycleSummary(data) {
  const s = data.summary
  if (!s || s.filesFixed == null) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ fontSize: 11, color: '#fcd34d' }}>{s.filesFixed} files fixed</div>
    </div>
  )
}

function renderWarningReviewSummary(data) {
  const s = data.summary
  if (!s || s.warningCount == null) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ fontSize: 11, color: '#fde68a' }}>{s.warningCount} warnings reviewed</div>
    </div>
  )
}

function renderSystemicAnalysisSummary(data) {
  const s = data.summary
  if (!s || (!s.patternCategory && s.affectedFiles == null)) return null

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
      {s.patternCategory && (
        <div style={{ fontSize: 10, color: '#c4b5fd' }}>
          Pattern: <span style={{ color: '#ddd6fe' }}>{s.patternCategory}</span>
        </div>
      )}
      {s.affectedFiles != null && (
        <div style={{ fontSize: 10, color: '#a78bfa' }}>{s.affectedFiles} files affected</div>
      )}
    </div>
  )
}

function renderSystemicFixSummary(data) {
  const s = data.summary
  if (!s || s.filesFixed == null) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ fontSize: 11, color: '#ddd6fe' }}>{s.filesFixed} files fixed (systemic)</div>
    </div>
  )
}

// ── Theme map ─────────────────────────────────────────────────────────────────

const SUB_CYCLE_THEME_MAP = {
  test_execution: {
    borderColor:             '#10b981',
    borderStyle:             'solid',
    bgColor:                 'rgba(16,185,129,0.06)',
    cornerColor:             'rgba(16,185,129,0.35)',
    icon:                    FlaskConical,
    countSuffix:             'event',
    collapsedLabel:          'Test Execution',
    collapsedTextColor:      '#6ee7b7',
    collapsedCountColor:     '#d1fae5',
    collapsedWidth:          260,
    renderCollapsedSummary:  renderTestExecutionSummary,
  },
  fix_cycle: {
    borderColor:             '#d97706',
    borderStyle:             'solid',
    bgColor:                 'rgba(217,119,6,0.06)',
    cornerColor:             'rgba(217,119,6,0.35)',
    icon:                    Wrench,
    countSuffix:             'fix',
    collapsedLabel:          'Fix Cycle',
    collapsedTextColor:      '#fcd34d',
    collapsedCountColor:     '#fef3c7',
    collapsedWidth:          260,
    renderCollapsedSummary:  renderFixCycleSummary,
  },
  warning_review: {
    borderColor:             '#f59e0b',
    borderStyle:             'solid',
    bgColor:                 'rgba(245,158,11,0.06)',
    cornerColor:             'rgba(245,158,11,0.35)',
    icon:                    AlertTriangle,
    countSuffix:             'warning',
    collapsedLabel:          'Warning Review',
    collapsedTextColor:      '#fde68a',
    collapsedCountColor:     '#fef3c7',
    collapsedWidth:          260,
    renderCollapsedSummary:  renderWarningReviewSummary,
  },
  systemic_analysis: {
    borderColor:             '#8b5cf6',
    borderStyle:             'solid',
    bgColor:                 'rgba(139,92,246,0.06)',
    cornerColor:             'rgba(139,92,246,0.35)',
    icon:                    Search,
    countSuffix:             'event',
    collapsedLabel:          'Systemic Analysis',
    collapsedTextColor:      '#c4b5fd',
    collapsedCountColor:     '#ede9fe',
    collapsedWidth:          260,
    renderCollapsedSummary:  renderSystemicAnalysisSummary,
  },
  systemic_fix: {
    borderColor:             '#a78bfa',
    borderStyle:             'solid',
    bgColor:                 'rgba(167,139,250,0.06)',
    cornerColor:             'rgba(167,139,250,0.35)',
    icon:                    Wrench,
    countSuffix:             'fix',
    collapsedLabel:          'Systemic Fix',
    collapsedTextColor:      '#ddd6fe',
    collapsedCountColor:     '#ede9fe',
    collapsedWidth:          260,
    renderCollapsedSummary:  renderSystemicFixSummary,
  },
  default: {
    borderColor:             '#6b7280',
    borderStyle:             'solid',
    bgColor:                 'rgba(107,114,128,0.06)',
    cornerColor:             'rgba(107,114,128,0.35)',
    icon:                    Box,
    countSuffix:             'event',
    collapsedLabel:          'Sub-cycle',
    collapsedTextColor:      '#d1d5db',
    collapsedCountColor:     '#f3f4f6',
  },
}

const SubCycleContainerNode = ({ data, ...props }) => {
  const theme = SUB_CYCLE_THEME_MAP[data.cycleType] || SUB_CYCLE_THEME_MAP.default
  return <CycleContainerNode data={data} {...props} theme={theme} />
}

export default memo(SubCycleContainerNode)
