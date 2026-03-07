import { memo } from 'react'
import { Wrench } from 'lucide-react'
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

const Sep = () => (
  <div style={{ height: 1, background: '#d97706', opacity: 0.25, margin: '2px 0' }} />
)

// ── Collapsed summary renderer ────────────────────────────────────────────────

function renderCollapsedSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor = s.status === 'success' ? '#10b981' : s.status === 'failed' ? '#ef4444' : '#f59e0b'
  const statusText = s.status === 'running' ? 'RUNNING' : s.status === 'success' ? 'SUCCESS' : 'FAILED'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {/* Status + duration */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {statusDot(statusColor)}
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {s.testCycleRows.length > 0 && <Sep />}

      {/* One row per test cycle */}
      {s.testCycleRows.map((tc, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11 }}>
          <span style={{ color: '#d1d5db', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
            {tc.testType}
          </span>
          {tc.passed != null && (
            <span style={{ color: tc.passed ? '#10b981' : '#ef4444', flexShrink: 0 }}>
              {tc.passed ? '✓' : '✗'}
            </span>
          )}
          {tc.filesFixed != null && tc.filesFixed > 0 && (
            <span style={{ color: '#9ca3af', flexShrink: 0 }}>{tc.filesFixed} fixed</span>
          )}
          {tc.iterations != null && (
            <span style={{ color: '#9ca3af', flexShrink: 0 }}>{tc.iterations} iters</span>
          )}
        </div>
      ))}

      {/* Env rebuild warning */}
      {s.envRebuildTriggered && (
        <>
          <Sep />
          <div style={{ fontSize: 10, color: '#f59e0b' }}>⚠ Env rebuild triggered</div>
        </>
      )}
    </div>
  )
}

// ── Theme ─────────────────────────────────────────────────────────────────────

const REPAIR_CYCLE_THEME = {
  borderColor:          '#d97706',
  bgColor:              'rgba(217, 119, 6, 0.14)',
  cornerColor:          'rgba(217,119,6,0.6)',
  icon:                 Wrench,
  countSuffix:          'test cycle',
  collapsedLabel:       'Repair Cycle',
  collapsedTextColor:   '#fcd34d',
  collapsedCountColor:  '#fef3c7',
  collapsedWidth:       340,
  renderCollapsedSummary,
}

const RepairCycleContainerNode = props => <CycleContainerNode {...props} theme={REPAIR_CYCLE_THEME} />

export default memo(RepairCycleContainerNode)
