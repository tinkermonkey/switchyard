/**
 * All collapsed summary renderers for cycle container nodes.
 * One named function per cycle type. Shared helpers defined once here.
 * Each renderer receives `data` and returns JSX (or null).
 * No theme logic — just content.
 */

// ── Shared helpers ────────────────────────────────────────────────────────────

export const fmtDur = s => {
  if (s == null) return '—'
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60), sec = s % 60
  return sec > 0 ? `${m}m ${sec}s` : `${m}m`
}

export const formatAgent = str =>
  str ? str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()) : ''

export const StatusDot = ({ color }) => (
  <div style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
)

const Sep = () => (
  <div style={{ height: 1, background: '#d97706', opacity: 0.25, margin: '2px 0' }} />
)

// ── Level-1 renderers ─────────────────────────────────────────────────────────

export function renderReviewCycleSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'approved'  ? '#10b981' :
    s.status === 'rejected'  ? '#ef4444' :
    s.status === 'escalated' ? '#f59e0b' : '#9333ea'

  const BAR_MAX_W = 180
  const totalDur = s.durationSeconds
  const iterations = s.iterations ?? []
  const maxIterDur = iterations.reduce((mx, it) => Math.max(mx, it.durationSeconds ?? 0), 0)

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>
          {s.status.toUpperCase()}
        </span>
        {totalDur != null && (
          <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>
            {fmtDur(totalDur)}
          </span>
        )}
      </div>

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

export function renderRepairCycleSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor = s.status === 'success' ? '#10b981' : s.status === 'failed' ? '#ef4444' : '#f59e0b'
  const statusText = s.status === 'running' ? 'RUNNING' : s.status === 'success' ? 'SUCCESS' : 'FAILED'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {s.testCycleRows.length > 0 && <Sep />}

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

      {s.envRebuildTriggered && (
        <>
          <Sep />
          <div style={{ fontSize: 10, color: '#f59e0b' }}>⚠ Env rebuild triggered</div>
        </>
      )}
    </div>
  )
}

export function renderPRReviewCycleSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'completed' ? '#10b981' :
    s.status === 'failed'    ? '#ef4444' : '#0ea5e9'

  const statusText = (s.finalStatus ?? s.status).toUpperCase()

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
      </div>
      <div style={{ fontSize: 11, color: '#7dd3fc' }}>
        {s.phaseCount} review phase{s.phaseCount !== 1 ? 's' : ''}
      </div>
    </div>
  )
}

export function renderConversationalLoopSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor = s.status === 'paused' ? '#9ca3af' : '#ec4899'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
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

// ── Level-2 renderers ─────────────────────────────────────────────────────────

export function renderReviewIterationSummary(data) {
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

export function renderRepairTestCycleSummary(data) {
  const s = data.summary
  if (!s) return null

  const passColor = s.passed ? '#10b981' : s.passed != null ? '#ef4444' : '#9ca3af'
  const passText = s.passed ? '✓ Passed' : s.passed != null ? '✗ Failed' : '…'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
        <span style={{ fontWeight: 700, color: passColor }}>{passText}</span>
        {s.filesFixed != null && s.filesFixed > 0 && (
          <span style={{ color: '#9ca3af' }}>{s.filesFixed} files fixed</span>
        )}
        {s.durationSeconds != null && (
          <span style={{ color: '#9ca3af', marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

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

      {s.warningsReviewed != null && s.warningsReviewed > 0 && (
        <div style={{ fontSize: 10, color: '#f59e0b' }}>{s.warningsReviewed} warnings reviewed</div>
      )}

      {s.hadSystemicFix && (
        <div style={{ fontSize: 10, color: '#a78bfa' }}>🔍 Systemic fix applied</div>
      )}
    </div>
  )
}

export function renderPRReviewPhaseSummary(data) {
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

// ── Level-3 renderers ─────────────────────────────────────────────────────────

export function renderTestExecutionSummary(data) {
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

export function renderFixCycleSummary(data) {
  const s = data.summary
  if (!s || s.filesFixed == null) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ fontSize: 11, color: '#fcd34d' }}>{s.filesFixed} files fixed</div>
    </div>
  )
}

export function renderWarningReviewSummary(data) {
  const s = data.summary
  if (!s || s.warningCount == null) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ fontSize: 11, color: '#fde68a' }}>{s.warningCount} warnings reviewed</div>
    </div>
  )
}

export function renderSystemicAnalysisSummary(data) {
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

export function renderSystemicFixSummary(data) {
  const s = data.summary
  if (!s || s.filesFixed == null) return null

  return (
    <div style={{ padding: '8px 12px' }}>
      <div style={{ fontSize: 11, color: '#ddd6fe' }}>{s.filesFixed} files fixed (systemic)</div>
    </div>
  )
}
