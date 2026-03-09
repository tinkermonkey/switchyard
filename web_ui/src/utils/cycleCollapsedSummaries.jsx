/**
 * All collapsed summary renderers for cycle container nodes.
 * One named function per cycle type. Shared helpers defined once here.
 * Each renderer receives `data` and returns JSX (or null).
 * No theme logic — just content.
 */

// ── Shared helpers ────────────────────────────────────────────────────────────

export const fmtDur = s => {
  if (s == null) return '—'
  s = Math.round(s)
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
  <div style={{ height: 1, background: '#21262d', margin: '3px 0' }} />
)

const Bar = ({ pct, color }) => (
  <div style={{ height: 4, background: '#21262d', borderRadius: 2, overflow: 'hidden' }}>
    <div style={{ height: '100%', width: `${Math.min(100, Math.max(0, pct))}%`, background: color, borderRadius: 2 }} />
  </div>
)

const BigNum = ({ value, label, color }) => (
  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
    <span style={{ fontSize: 18, fontWeight: 700, color, lineHeight: 1 }}>{value ?? '—'}</span>
    <span style={{ fontSize: 8, color: '#7d8590', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</span>
  </div>
)

// ── Level-1 renderers ─────────────────────────────────────────────────────────

export function renderReviewCycleSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'approved'  ? '#10b981' :
    s.status === 'rejected'  ? '#ef4444' :
    s.status === 'escalated' ? '#f59e0b' : '#9333ea'

  const iterations = s.iterations ?? []
  const maxIterDur = iterations.reduce((mx, it) => Math.max(mx, it.durationSeconds ?? 0), 0)

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
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

      {/* Agent rows */}
      {s.makerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: '#7d8590', minWidth: 52 }}>Maker</span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.makerAgent)}</span>
        </div>
      )}
      {s.reviewerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: '#7d8590', minWidth: 52 }}>Reviewer</span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.reviewerAgent)}</span>
        </div>
      )}

      {iterations.length > 0 && (
        <>
          <Sep />
          {/* Iteration count callout — inline: "2 / 5 ITERATIONS" */}
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
            <span style={{ fontSize: 18, fontWeight: 700, color: '#9333ea', lineHeight: 1 }}>
              {s.totalIterations ?? '—'}
            </span>
            {s.maxIterations != null && (
              <span style={{ fontSize: 11, color: '#7d8590' }}>/{s.maxIterations}</span>
            )}
            <span style={{ fontSize: 8, color: '#7d8590', textTransform: 'uppercase', letterSpacing: '0.06em', marginLeft: 2 }}>
              ITERATIONS
            </span>
          </div>

          {/* Proportional bars per iteration */}
          {iterations.map((iter, idx) => {
            const isRunning = iter.durationSeconds == null && s.status === 'running' && idx === iterations.length - 1
            const pct = iter.durationSeconds != null && maxIterDur > 0
              ? (iter.durationSeconds / maxIterDur) * 100
              : isRunning ? 25 : 2

            return (
              <div key={iter.number} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 9, color: '#7d8590', minWidth: 28 }}>
                  Iter {iter.number}
                </span>
                <div style={{ flex: 1 }}>
                  <Bar pct={pct} color="#9333ea" />
                </div>
                <span style={{ fontSize: 9, color: '#7d8590', minWidth: 28, textAlign: 'right' }}>
                  {fmtDur(iter.durationSeconds)}
                </span>
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}

export function renderRepairCycleSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor = s.status === 'success' ? '#10b981' : s.status === 'failed' ? '#ef4444' : '#f59e0b'
  const statusText = s.status === 'running' ? 'RUNNING' : s.status === 'success' ? 'SUCCESS' : 'FAILED'

  const totalPass   = s.testCycleRows.filter(tc => tc.passed).length
  const totalFixed  = s.testCycleRows.reduce((n, tc) => n + (tc.filesFixed ?? 0), 0)
  const cycleCount  = s.testCycleRows.length

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {cycleCount > 0 && (
        <>
          <Sep />
          {/* Stat row */}
          <div style={{ display: 'flex', justifyContent: 'space-around' }}>
            <BigNum value={totalPass}  label="PASS"   color="#10b981" />
            <BigNum value={totalFixed} label="FIXED"  color="#fcd34d" />
            <BigNum value={cycleCount} label="CYCLES" color="#d97706" />
          </div>
          {/* Per-type rows */}
          {s.testCycleRows.map((tc, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10 }}>
              <span style={{ color: '#fcd34d', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
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
                <span style={{ color: '#9ca3af', flexShrink: 0 }}>{tc.iterations} iter{tc.iterations !== 1 ? 's' : ''}</span>
              )}
            </div>
          ))}
        </>
      )}

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
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: '#9ca3af', marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      <Sep />

      {/* Stat row: phases / issues / ci fail */}
      <div style={{ display: 'flex', justifyContent: 'space-around' }}>
        <BigNum value={s.phaseCount}  label="PHASES"  color="#0ea5e9" />
        <BigNum value={s.issueCount}  label="ISSUES"  color="#7dd3fc" />
        <BigNum value={s.ciFailCount} label="CI FAIL" color="#f87171" />
      </div>
    </div>
  )
}

export function renderConversationalLoopSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor = '#ec4899'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
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

      <Sep />

      {/* Exchange count callout */}
      <BigNum value={s.exchangeCount} label="EXCHANGES" color="#ec4899" />

      {(s.agentName || s.pausedReason) && <Sep />}

      {s.agentName && (
        <div style={{ fontSize: 10, color: '#9ca3af' }}>
          Agent: {formatAgent(s.agentName)}
        </div>
      )}
      {s.pausedReason && (
        <div style={{ fontSize: 10, color: '#f59e0b' }}>{s.pausedReason}</div>
      )}
    </div>
  )
}

// ── Status progression renderer ───────────────────────────────────────────────

const STATUS_COLORS = {
  'In Progress':  { bg: 'rgba(59,130,246,0.15)', border: 'rgba(59,130,246,0.4)', text: '#93c5fd' },
  'In Review':    { bg: 'rgba(147,51,234,0.15)', border: 'rgba(147,51,234,0.4)', text: '#c4b5fd' },
  'Done':         { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,0.4)', text: '#6ee7b7' },
  'Completed':    { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,0.4)', text: '#6ee7b7' },
  'To Do':        { bg: 'rgba(107,114,128,0.15)', border: 'rgba(107,114,128,0.4)', text: '#9ca3af' },
  'Backlog':      { bg: 'rgba(107,114,128,0.15)', border: 'rgba(107,114,128,0.4)', text: '#9ca3af' },
  'Blocked':      { bg: 'rgba(239,68,68,0.15)',  border: 'rgba(239,68,68,0.4)',  text: '#fca5a5' },
}

function StatusPill({ label }) {
  const fmt = str => str ? str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()) : '—'
  const formatted = fmt(label)
  const theme = STATUS_COLORS[formatted] ?? { bg: 'rgba(34,197,94,0.12)', border: 'rgba(34,197,94,0.35)', text: '#86efac' }
  return (
    <div style={{
      background: theme.bg,
      border: `1px solid ${theme.border}`,
      borderRadius: 6,
      padding: '4px 10px',
      fontSize: 11,
      color: theme.text,
      fontWeight: 600,
      whiteSpace: 'nowrap',
      letterSpacing: '0.01em',
    }}>
      {formatted}
    </div>
  )
}

export function renderStatusProgressionSummary(data) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'completed'  ? '#22c55e' :
    s.status === 'failed'     ? '#ef4444' : '#86efac'

  const statusLabel =
    s.status === 'completed'  ? 'MOVED' :
    s.status === 'failed'     ? 'FAILED' : 'MOVING…'

  return (
    <div style={{ padding: '10px 14px', display: 'flex', flexDirection: 'column', gap: 7 }}>
      {/* From → To graphic */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusPill label={s.fromStatus ?? '?'} />
        <div style={{ fontSize: 16, color: '#22c55e', fontWeight: 700, lineHeight: 1, flexShrink: 0 }}>→</div>
        <StatusPill label={s.toStatus ?? '?'} />
        {s.durationSeconds != null && (
          <span style={{ fontSize: 10, color: '#6b7280', marginLeft: 'auto' }}>
            {fmtDur(s.durationSeconds)}
          </span>
        )}
      </div>

      {/* Status + trigger */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 10, fontWeight: 700, color: statusColor }}>{statusLabel}</span>
        {s.trigger && (
          <span style={{ fontSize: 9, color: '#4b5563', marginLeft: 3 }}>
            via {s.trigger.replace(/_/g, ' ')}
          </span>
        )}
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
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: '#7d8590', minWidth: 52 }}>Maker</span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.makerAgent)}</span>
        </div>
      )}
      {s.reviewerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: '#7d8590', minWidth: 52 }}>Reviewer</span>
          <span style={{ color: '#c4b5fd' }}>{formatAgent(s.reviewerAgent)}</span>
        </div>
      )}
      {s.eventCount > 0 && (
        <>
          <Sep />
          <div style={{ fontSize: 10, color: '#7d8590' }}>{s.eventCount} events</div>
        </>
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
      {/* Header row: pass/fail + type + duration */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontWeight: 700, color: passColor }}>{passText}</span>
          {s.testType && (
            <span style={{ color: '#fcd34d' }}>{s.testType}</span>
          )}
        </div>
        {s.durationSeconds != null && (
          <span style={{ color: '#7d8590' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      <Sep />

      {/* Test counts */}
      {s.testResultRow && (s.testResultRow.passedCount != null || s.testResultRow.failedCount != null) && (
        <div style={{ fontSize: 10, color: '#9ca3af' }}>
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

      {/* Files fixed + iterations */}
      {((s.filesFixed != null && s.filesFixed > 0) || s.iterationsUsed != null) && (
        <div style={{ fontSize: 10, color: '#9ca3af' }}>
          {s.filesFixed != null && s.filesFixed > 0 && `${s.filesFixed} files fixed`}
          {s.filesFixed != null && s.filesFixed > 0 && s.iterationsUsed != null && ' · '}
          {s.iterationsUsed != null && `${s.iterationsUsed} iter${s.iterationsUsed !== 1 ? 's' : ''}`}
        </div>
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

  const phaseName = s.phaseName
  const nameLC = (phaseName ?? '').toLowerCase()

  let detailLine = null
  if (nameLC.includes('ci') || nameLC.includes('check')) {
    detailLine = `${s.failuresFound ?? 0} failures · ${s.eventCount} pending`
  } else if (nameLC.includes('consolidat')) {
    detailLine = `${s.issuesFound ?? 0} issues found`
  } else if (phaseName) {
    // Generic code review phase — show text collected indicator
    const icon = s.textCollected ? '✓' : '✗'
    const iconColor = s.textCollected ? '#10b981' : '#ef4444'
    detailLine = (
      <span>
        <span style={{ color: iconColor }}>{icon}</span>
        <span style={{ color: '#9ca3af' }}> text collected · {s.eventCount} events</span>
      </span>
    )
  } else {
    detailLine = `${s.eventCount} event${s.eventCount !== 1 ? 's' : ''}`
  }

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {/* Phase name + duration */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: '#7dd3fc' }}>
          {phaseName ?? `Phase ${s.phaseNumber}`}
        </span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 10, color: '#7d8590' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {/* Detail line */}
      {detailLine != null && (
        <div style={{ fontSize: 10, color: '#9ca3af' }}>{detailLine}</div>
      )}
    </div>
  )
}

// ── Level-3 renderers ─────────────────────────────────────────────────────────

export function renderTestExecutionSummary(data) {
  const s = data.summary
  if (!s) return null
  if (s.testPassedCount == null && s.testFailedCount == null) return null

  const passed = s.testPassedCount ?? 0
  const failed = s.testFailedCount ?? 0
  const total  = passed + failed
  const pct    = total > 0 ? (passed / total) * 100 : 0

  const failures = s.failuresList ?? []

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Pass / fail counts + pct */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11 }}>
        <span style={{ color: '#6ee7b7', fontWeight: 700 }}>✓ {passed}</span>
        <span style={{ color: '#f87171', fontWeight: 700 }}>✗ {failed}</span>
        {total > 0 && (
          <span style={{ color: '#7d8590', marginLeft: 'auto' }}>
            {Math.round(pct)}%
          </span>
        )}
      </div>

      {/* Progress bar */}
      <Bar pct={pct} color="#10b981" />

      {/* Failure list */}
      {failures.length > 0 && (
        <>
          <Sep />
          {failures.slice(0, 3).map((f, i) => {
            const label = typeof f === 'string' ? f : (f.name ?? f.test ?? String(f))
            const basename = label.includes('/') ? label.split('/').pop() : label
            return (
              <div key={i} style={{ fontSize: 10, color: '#f87171' }}>
                · <span style={{ color: '#7d8590' }}>{basename}</span>
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}

export function renderFixCycleSummary(data) {
  const s = data.summary
  if (!s || s.filesFixed == null) return null

  const files = s.fixedFilesList ?? []

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Big number callout */}
      <BigNum value={s.filesFixed} label="FILES FIXED" color="#fcd34d" />

      {/* File list */}
      {files.length > 0 && (
        <>
          <Sep />
          {files.slice(0, 3).map((f, i) => {
            const path = typeof f === 'string' ? f : (f.path ?? f.file ?? String(f))
            const basename = path.includes('/') ? path.split('/').pop() : path
            return (
              <div key={i} style={{ fontSize: 10, color: '#9ca3af' }}>· {basename}</div>
            )
          })}
        </>
      )}
    </div>
  )
}

export function renderWarningReviewSummary(data) {
  const s = data.summary
  if (!s || s.warningCount == null) return null

  return (
    <div style={{ padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ fontSize: 14, color: '#f59e0b' }}>⚠</span>
      <BigNum value={s.warningCount} label="WARNINGS" color="#fde68a" />
    </div>
  )
}

export function renderSystemicAnalysisSummary(data) {
  const s = data.summary
  if (!s || (!s.patternCategory && s.affectedFiles == null)) return null

  const title = s.issueDescription ?? 'Systemic Code Issue'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#c4b5fd' }}>⚙ {title}</div>
      {(s.patternCategory || s.affectedFiles != null) && (
        <div style={{ fontSize: 10, color: '#9ca3af' }}>
          {s.patternCategory ?? ''}
          {s.patternCategory && s.affectedFiles != null && ' · '}
          {s.affectedFiles != null && `${s.affectedFiles} files affected`}
        </div>
      )}
    </div>
  )
}

export function renderSystemicFixSummary(data) {
  const s = data.summary
  if (!s) return null

  const isComplete = s.outcome === 'complete' || (s.filesFixed != null && s.filesFixed > 0)

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: isComplete ? '#10b981' : '#7d8590' }}>
        {isComplete ? '✓ Tests passing' : '… Running'}{' '}
        <span style={{ color: '#7d8590', fontWeight: 400 }}>(systemic)</span>
      </div>
      {(s.patternCategory || s.attemptCount != null) && (
        <div style={{ fontSize: 10, color: '#9ca3af' }}>
          {s.patternCategory ?? ''}
          {s.patternCategory && s.attemptCount != null && ' · '}
          {s.attemptCount != null && `${s.attemptCount} attempt${s.attemptCount !== 1 ? 's' : ''}`}
        </div>
      )}
    </div>
  )
}
