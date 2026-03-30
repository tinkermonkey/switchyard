/**
 * All collapsed summary renderers for cycle container nodes.
 * One named function per cycle type. Shared helpers defined once here.
 * Each renderer receives `data` and `isDark` and returns JSX (or null).
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

const Sep = ({ isDark }) => (
  <div style={{ height: 1, background: isDark ? '#21262d' : '#d0d7de', margin: '3px 0' }} />
)

const Bar = ({ pct, color, isDark }) => (
  <div style={{ height: 4, background: isDark ? '#21262d' : '#e5e7eb', borderRadius: 2, overflow: 'hidden' }}>
    <div style={{ height: '100%', width: `${Math.min(100, Math.max(0, pct))}%`, background: color, borderRadius: 2 }} />
  </div>
)

const BigNum = ({ value, label, color, isDark }) => (
  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
    <span style={{ fontSize: 18, fontWeight: 700, color, lineHeight: 1 }}>{value ?? '—'}</span>
    <span style={{ fontSize: 8, color: isDark ? '#7d8590' : '#57606a', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</span>
  </div>
)

// ── Level-1 renderers ─────────────────────────────────────────────────────────

export function renderReviewCycleSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'approved'  ? '#10b981' :
    s.isFailure              ? '#ef4444' :
    '#9333ea'

  const iterations = s.iterations ?? []
  const maxIterDur = iterations.reduce((mx, it) => Math.max(mx, it.durationSeconds ?? 0), 0)

  const secondary = isDark ? '#9ca3af' : '#6e7781'
  const muted     = isDark ? '#7d8590' : '#57606a'
  const agentColor = isDark ? '#c4b5fd' : '#6d28d9'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>
          {s.status.toUpperCase()}
        </span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: secondary, marginLeft: 'auto' }}>
            {fmtDur(s.durationSeconds)}
          </span>
        )}
      </div>

      {/* Agent rows */}
      {s.makerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: muted, minWidth: 52 }}>Maker</span>
          <span style={{ color: agentColor }}>{formatAgent(s.makerAgent)}</span>
        </div>
      )}
      {s.reviewerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: muted, minWidth: 52 }}>Reviewer</span>
          <span style={{ color: agentColor }}>{formatAgent(s.reviewerAgent)}</span>
        </div>
      )}

      {iterations.length > 0 && (
        <>
          <Sep isDark={isDark} />
          {/* Iteration count callout — inline: "2 / 5 ITERATIONS" */}
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
            <span style={{ fontSize: 18, fontWeight: 700, color: '#9333ea', lineHeight: 1 }}>
              {s.totalIterations ?? '—'}
            </span>
            {s.maxIterations != null && (
              <span style={{ fontSize: 11, color: muted }}>/{s.maxIterations}</span>
            )}
            <span style={{ fontSize: 8, color: muted, textTransform: 'uppercase', letterSpacing: '0.06em', marginLeft: 2 }}>
              ITERATIONS
            </span>
          </div>

          {/* Proportional bars per iteration */}
          {iterations.map((iter, idx) => {
            const isLast = idx === iterations.length - 1
            const isLastFailed = s.isFailure && isLast
            const isRunning = iter.durationSeconds == null && s.status === 'running' && isLast
            const pct = iter.durationSeconds != null && maxIterDur > 0
              ? (iter.durationSeconds / maxIterDur) * 100
              : isRunning ? 25 : 2
            const barColor = isLastFailed ? '#ef4444' : '#9333ea'

            return (
              <div key={iter.number} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 9, color: isLastFailed ? '#ef4444' : muted, minWidth: 28, fontWeight: isLastFailed ? 600 : 400 }}>
                  Iter {iter.number}
                </span>
                <div style={{ flex: 1 }}>
                  <Bar pct={pct} color={barColor} isDark={isDark} />
                </div>
                {isLastFailed
                  ? <span style={{ fontSize: 9, color: '#ef4444', fontWeight: 700, minWidth: 28, textAlign: 'right' }}>✗</span>
                  : <span style={{ fontSize: 9, color: muted, minWidth: 28, textAlign: 'right' }}>{fmtDur(iter.durationSeconds)}</span>
                }
              </div>
            )
          })}
          {s.isFailure && s.completionReason && (
            <div style={{ fontSize: 10, color: isDark ? '#f87171' : '#dc2626', marginTop: 1 }}>
              ✗ {s.completionReason}
            </div>
          )}
        </>
      )}
    </div>
  )
}

export function renderRepairCycleSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const statusColor = s.status === 'success' ? '#10b981' : s.status === 'failed' ? '#ef4444' : '#f59e0b'
  const statusText = s.status === 'running' ? 'RUNNING' : s.status === 'success' ? 'SUCCESS' : 'FAILED'

  const totalPass   = s.testCycleRows.filter(tc => tc.passed).length
  const totalFixed  = s.testCycleRows.reduce((n, tc) => n + (tc.filesFixed ?? 0), 0)
  const cycleCount  = s.testCycleRows.length

  const secondary   = isDark ? '#9ca3af' : '#57606a'
  const fixedColor  = isDark ? '#fcd34d' : '#92400e'
  const testTypeColor = isDark ? '#fcd34d' : '#92400e'
  const envWarnColor  = isDark ? '#f59e0b' : '#d97706'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: secondary, marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {cycleCount > 0 && (
        <>
          <Sep isDark={isDark} />
          {/* Stat row */}
          <div style={{ display: 'flex', justifyContent: 'space-around' }}>
            <BigNum value={totalPass}  label="PASS"   color="#10b981" isDark={isDark} />
            <BigNum value={totalFixed} label="FIXED"  color={fixedColor} isDark={isDark} />
            <BigNum value={cycleCount} label="CYCLES" color="#d97706" isDark={isDark} />
          </div>
          {/* Per-type rows */}
          {s.testCycleRows.map((tc, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10 }}>
              <span style={{ color: testTypeColor, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
                {tc.testType}
              </span>
              {tc.passed != null && (
                <span style={{ color: tc.passed ? '#10b981' : '#ef4444', flexShrink: 0 }}>
                  {tc.passed ? '✓' : '✗'}
                </span>
              )}
              {tc.filesFixed != null && tc.filesFixed > 0 && (
                <span style={{ color: secondary, flexShrink: 0 }}>{tc.filesFixed} fixed</span>
              )}
              {tc.iterations != null && (
                <span style={{ color: secondary, flexShrink: 0 }}>{tc.iterations} iter{tc.iterations !== 1 ? 's' : ''}</span>
              )}
            </div>
          ))}
        </>
      )}

      {s.envRebuildTriggered && (
        <>
          <Sep isDark={isDark} />
          <div style={{ fontSize: 10, color: envWarnColor }}>⚠ Env rebuild triggered</div>
        </>
      )}
    </div>
  )
}

export function renderPRReviewCycleSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const statusColor =
    s.status === 'completed' ? '#10b981' :
    s.status === 'failed'    ? '#ef4444' : '#0ea5e9'

  const statusText = (s.finalStatus ?? s.status).toUpperCase()

  const secondary   = isDark ? '#9ca3af' : '#6e7781'
  const phaseColor  = isDark ? '#0ea5e9' : '#0369a1'
  const issueColor  = isDark ? '#7dd3fc' : '#0284c7'
  const ciFailColor = isDark ? '#f87171' : '#dc2626'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>{statusText}</span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: secondary, marginLeft: 'auto' }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      <Sep isDark={isDark} />

      {/* Stat row: phases / issues / ci fail */}
      <div style={{ display: 'flex', justifyContent: 'space-around' }}>
        <BigNum value={s.phaseCount}  label="PHASES"  color={phaseColor}  isDark={isDark} />
        <BigNum value={s.issueCount}  label="ISSUES"  color={issueColor}  isDark={isDark} />
        <BigNum value={s.ciFailCount} label="CI FAIL" color={ciFailColor} isDark={isDark} />
      </div>
    </div>
  )
}

export function renderConversationalLoopSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const statusColor  = '#ec4899'
  const secondary    = isDark ? '#9ca3af' : '#57606a'
  const exchangeColor = isDark ? '#ec4899' : '#be185d'
  const pausedColor  = isDark ? '#f59e0b' : '#d97706'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Status row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 11, fontWeight: 700, color: statusColor }}>
          {s.status.toUpperCase()}
        </span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 11, color: isDark ? '#9ca3af' : '#6e7781', marginLeft: 'auto' }}>
            {fmtDur(s.durationSeconds)}
          </span>
        )}
      </div>

      <Sep isDark={isDark} />

      {/* Exchange count callout */}
      <BigNum value={s.exchangeCount} label="EXCHANGES" color={exchangeColor} isDark={isDark} />

      {(s.agentName || s.pausedReason) && <Sep isDark={isDark} />}

      {s.agentName && (
        <div style={{ fontSize: 10, color: secondary }}>
          Agent: {formatAgent(s.agentName)}
        </div>
      )}
      {s.pausedReason && (
        <div style={{ fontSize: 10, color: pausedColor }}>{s.pausedReason}</div>
      )}
    </div>
  )
}

// ── Status progression renderer ───────────────────────────────────────────────

export function renderStatusProgressionSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const fmt = str => str ? str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()) : '?'

  const statusColor = isDark
    ? (s.status === 'completed' ? '#22c55e' : s.status === 'failed' ? '#ef4444' : '#86efac')
    : (s.status === 'completed' ? '#15803d' : s.status === 'failed' ? '#dc2626' : '#166534')

  const titleColor = isDark
    ? (s.status === 'completed' ? '#86efac' : s.status === 'failed' ? '#fca5a5' : '#86efac')
    : (s.status === 'completed' ? '#166534' : s.status === 'failed' ? '#dc2626' : '#166534')

  const statusLabel =
    s.status === 'completed' ? 'MOVED' :
    s.status === 'failed'    ? 'FAILED' : 'MOVING…'

  const durStr = s.durationSeconds != null ? `  ${fmtDur(s.durationSeconds)}` : ''

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: titleColor }}>
        {fmt(s.fromStatus ?? '?')} → {fmt(s.toStatus ?? '?')}
      </div>
      <div style={{ fontSize: 10, fontWeight: 700, color: statusColor }}>
        ● {statusLabel}{durStr}
      </div>
    </div>
  )
}

// ── Level-2 renderers ─────────────────────────────────────────────────────────

export function renderReviewIterationSummary(data, isDark) {
  const s = data.summary
  if (!s || (!s.makerAgent && !s.reviewerAgent)) return null

  const muted      = isDark ? '#7d8590' : '#57606a'
  const agentColor = isDark ? '#c4b5fd' : '#6d28d9'
  const failColor  = isDark ? '#f87171' : '#dc2626'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {s.isFailed && (
        <div style={{ fontSize: 10, fontWeight: 700, color: failColor }}>✗ Escalated</div>
      )}
      {s.makerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: muted, minWidth: 52 }}>Maker</span>
          <span style={{ color: agentColor }}>{formatAgent(s.makerAgent)}</span>
        </div>
      )}
      {s.reviewerAgent && (
        <div style={{ fontSize: 10, display: 'flex', gap: 4 }}>
          <span style={{ color: muted, minWidth: 52 }}>Reviewer</span>
          <span style={{ color: agentColor }}>{formatAgent(s.reviewerAgent)}</span>
        </div>
      )}
      {s.eventCount > 0 && (
        <>
          <Sep isDark={isDark} />
          <div style={{ fontSize: 10, color: muted }}>{s.eventCount} events</div>
        </>
      )}
    </div>
  )
}

export function renderRepairTestCycleSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const passColor = s.passed
    ? (isDark ? '#10b981' : '#047857')
    : s.passed != null
      ? (isDark ? '#ef4444' : '#dc2626')
      : (isDark ? '#9ca3af' : '#57606a')
  const passText = s.passed ? '✓ Passed' : s.passed != null ? '✗ Failed' : '…'

  const muted        = isDark ? '#7d8590' : '#6e7781'
  const secondary    = isDark ? '#9ca3af' : '#57606a'
  const testTypeColor = isDark ? '#fcd34d' : '#92400e'
  const systemicColor = isDark ? '#a78bfa' : '#7c3aed'

  const tr = s.testResultRow
  const hasTestResult = tr && (tr.passedCount != null || tr.failedCount != null)
  const hasFooter = (s.filesFixed != null && s.filesFixed > 0) || s.iterationsUsed != null

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {/* Header row: pass/fail + type + duration */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontWeight: 700, color: passColor }}>{passText}</span>
          {s.testType && (
            <span style={{ color: testTypeColor }}>{s.testType}</span>
          )}
        </div>
        {s.durationSeconds != null && (
          <span style={{ color: muted }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {/* BigNum test counts: N pass | N fail | N warn */}
      {hasTestResult && (
        <div style={{ display: 'flex', gap: 10 }}>
          {tr.passedCount != null && (
            <BigNum value={tr.passedCount} label="pass" color={isDark ? '#34d399' : '#047857'} isDark={isDark} />
          )}
          {tr.failedCount != null && (
            <BigNum
              value={tr.failedCount}
              label="fail"
              color={(tr.failedCount ?? 0) > 0 ? (isDark ? '#f87171' : '#dc2626') : (isDark ? '#6ee7b7' : '#047857')}
              isDark={isDark}
            />
          )}
          {tr.warningsCount != null && (
            <BigNum value={tr.warningsCount} label="warn" color={isDark ? '#fcd34d' : '#92400e'} isDark={isDark} />
          )}
        </div>
      )}

      {/* Footer: files fixed + iterations */}
      {hasFooter && (
        <>
          <Sep isDark={isDark} />
          <div style={{ fontSize: 10, color: secondary }}>
            {s.filesFixed != null && s.filesFixed > 0 && `${s.filesFixed} files fixed`}
            {s.filesFixed != null && s.filesFixed > 0 && s.iterationsUsed != null && ' · '}
            {s.iterationsUsed != null && `${s.iterationsUsed} iter${s.iterationsUsed !== 1 ? 's' : ''}`}
          </div>
        </>
      )}

      {s.hadSystemicFix && (
        <div style={{ fontSize: 10, color: systemicColor }}>🔍 Systemic fix applied</div>
      )}
    </div>
  )
}

export function renderPRReviewPhaseSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const phaseName = s.phaseName
  const nameLC = (phaseName ?? '').toLowerCase()

  const muted     = isDark ? '#7d8590' : '#6e7781'
  const secondary = isDark ? '#9ca3af' : '#57606a'
  const phaseNameColor = isDark ? '#7dd3fc' : '#0369a1'

  let detailLine = null
  if (nameLC.includes('ci') || nameLC.includes('check')) {
    detailLine = `${s.failuresFound ?? 0} failures · ${s.eventCount} pending`
  } else if (nameLC.includes('consolidat')) {
    detailLine = `${s.issuesFound ?? 0} issues found`
  } else if (phaseName) {
    const icon = s.textCollected ? '✓' : '✗'
    const iconColor = s.textCollected ? (isDark ? '#10b981' : '#047857') : (isDark ? '#ef4444' : '#dc2626')
    detailLine = (
      <span>
        <span style={{ color: iconColor }}>{icon}</span>
        <span style={{ color: secondary }}> text collected · {s.eventCount} events</span>
      </span>
    )
  } else {
    detailLine = `${s.eventCount} event${s.eventCount !== 1 ? 's' : ''}`
  }

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      {/* Phase name + duration */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: phaseNameColor }}>
          {phaseName ?? `Phase ${s.phaseNumber}`}
        </span>
        {s.durationSeconds != null && (
          <span style={{ fontSize: 10, color: muted }}>{fmtDur(s.durationSeconds)}</span>
        )}
      </div>

      {/* Detail line */}
      {detailLine != null && (
        <div style={{ fontSize: 10, color: secondary }}>{detailLine}</div>
      )}
    </div>
  )
}

// ── Level-3 renderers ─────────────────────────────────────────────────────────

export function renderTestExecutionSummary(data, isDark) {
  const s = data.summary
  if (!s) return null
  if (s.testPassedCount == null && s.testFailedCount == null) return null

  const passed = s.testPassedCount ?? 0
  const failed = s.testFailedCount ?? 0
  const total  = passed + failed
  const pct    = total > 0 ? (passed / total) * 100 : 0

  const failures = s.failuresList ?? []

  const passColor    = isDark ? '#6ee7b7' : '#047857'
  const failNumColor = failed > 0 ? (isDark ? '#f87171' : '#dc2626') : (isDark ? '#6ee7b7' : '#047857')
  const failColor    = isDark ? '#f87171' : '#dc2626'
  const muted        = isDark ? '#7d8590' : '#6e7781'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Pass / fail BigNums + pct */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <BigNum value={passed} label="pass" color={passColor} isDark={isDark} />
        <BigNum value={failed} label="fail" color={failNumColor} isDark={isDark} />
        {total > 0 && (
          <span style={{ fontSize: 14, fontWeight: 700, color: muted, marginLeft: 'auto', alignSelf: 'center' }}>
            {Math.round(pct)}%
          </span>
        )}
      </div>

      {/* Progress bar */}
      <Bar pct={pct} color="#10b981" isDark={isDark} />

      {/* Failure list: test name (red) + file basename (gray) */}
      {failures.length > 0 && (
        <>
          <Sep isDark={isDark} />
          {failures.slice(0, 3).map((f, i) => {
            const rawName = typeof f === 'string' ? f : (f.name ?? f.test ?? String(f))
            // Extract method name after '::' (Python test pattern), fallback to full name
            const testLabel = rawName.includes('::') ? rawName.split('::').pop() : rawName
            const filePath = typeof f === 'object' ? (f.file ?? f.path ?? null) : null
            const fileBase = filePath ? filePath.split('/').pop() : null
            return (
              <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
                <div style={{ fontSize: 10, color: failColor, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {testLabel}
                </div>
                {fileBase && (
                  <div style={{ fontSize: 10, color: muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    · {fileBase}
                  </div>
                )}
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}

export function renderFixCycleSummary(data, isDark) {
  const s = data.summary
  if (!s || s.filesFixed == null) return null

  const files = s.fixedFilesList ?? []
  const fixedColor = isDark ? '#fcd34d' : '#92400e'
  const secondary  = isDark ? '#9ca3af' : '#57606a'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {/* Big number callout */}
      <BigNum value={s.filesFixed} label="FILES FIXED" color={fixedColor} isDark={isDark} />

      {/* File list */}
      {files.length > 0 && (
        <>
          <Sep isDark={isDark} />
          {files.slice(0, 3).map((f, i) => {
            const path = typeof f === 'string' ? f : (f.path ?? f.file ?? String(f))
            const basename = path.includes('/') ? path.split('/').pop() : path
            return (
              <div key={i} style={{ fontSize: 10, color: secondary }}>· {basename}</div>
            )
          })}
        </>
      )}
    </div>
  )
}

export function renderWarningReviewSummary(data, isDark) {
  const s = data.summary
  if (!s || s.warningCount == null) return null

  const warnColor = isDark ? '#fde68a' : '#92400e'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ fontSize: 14, color: isDark ? '#f59e0b' : '#d97706' }}>⚠</span>
      <BigNum value={s.warningCount} label="WARNINGS" color={warnColor} isDark={isDark} />
    </div>
  )
}

export function renderSystemicAnalysisSummary(data, isDark) {
  const s = data.summary
  if (!s || (!s.patternCategory && s.affectedFiles == null)) return null

  const title = s.issueDescription ?? 'Systemic Code Issue'
  const titleColor = isDark ? '#c4b5fd' : '#6d28d9'
  const secondary  = isDark ? '#9ca3af' : '#57606a'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: titleColor }}>⚙ {title}</div>
      {(s.patternCategory || s.affectedFiles != null) && (
        <div style={{ fontSize: 10, color: secondary }}>
          {s.patternCategory ?? ''}
          {s.patternCategory && s.affectedFiles != null && ' · '}
          {s.affectedFiles != null && `${s.affectedFiles} files affected`}
        </div>
      )}
    </div>
  )
}

export function renderSystemicFixSummary(data, isDark) {
  const s = data.summary
  if (!s) return null

  const isComplete = s.outcome === 'complete' || (s.filesFixed != null && s.filesFixed > 0)

  const successColor = isDark ? '#10b981' : '#047857'
  const runningColor = isDark ? '#7d8590' : '#57606a'
  const mutedColor   = isDark ? '#7d8590' : '#57606a'
  const secondary    = isDark ? '#9ca3af' : '#57606a'

  return (
    <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: isComplete ? successColor : runningColor }}>
        {isComplete ? '✓ Tests passing' : '… Running'}{' '}
        <span style={{ color: mutedColor, fontWeight: 400 }}>(systemic)</span>
      </div>
      {(s.patternCategory || s.attemptCount != null) && (
        <div style={{ fontSize: 10, color: secondary }}>
          {s.patternCategory ?? ''}
          {s.patternCategory && s.attemptCount != null && ' · '}
          {s.attemptCount != null && `${s.attemptCount} attempt${s.attemptCount !== 1 ? 's' : ''}`}
        </div>
      )}
    </div>
  )
}

// ── Agent execution renderer ──────────────────────────────────────────────────

export function renderAgentExecutionSummary(data, isDark) {
  const summary = data.summary ?? {}
  const { status, durationMs, inputTokens, outputTokens, tools } = summary
  const { containsActiveAgent, iterationCount } = data

  const effectiveStatus = containsActiveAgent || status === 'running' ? 'running' : (status ?? 'completed')

  const statusColor =
    effectiveStatus === 'running'     ? (isDark ? '#58a6ff' : '#0969da') :
    effectiveStatus === 'completed'   ? (isDark ? '#3fb950' : '#2da44e') :
    effectiveStatus === 'failed'      ? (isDark ? '#f85149' : '#cf222e') :
    effectiveStatus === 'interrupted' ? (isDark ? '#d29055' : '#bc4c00') :
    (isDark ? '#6e7681' : '#57606a')

  const statusLabel = {
    running: 'RUNNING',
    completed: 'DONE',
    failed: 'FAILED',
    interrupted: 'KILLED',
  }[effectiveStatus] ?? 'DONE'

  const durationStr = durationMs != null ? fmtDur(Math.round(durationMs / 1000)) : null
  const totalTokens = (inputTokens ?? 0) + (outputTokens ?? 0)
  const tokensStr = totalTokens > 0
    ? totalTokens >= 1_000_000 ? `${(totalTokens / 1_000_000).toFixed(1)}M`
      : totalTokens >= 1_000 ? `${(totalTokens / 1_000).toFixed(1)}k`
      : String(totalTokens)
    : null

  const muted = isDark ? '#7d8590' : '#57606a'

  return (
    <div style={{ padding: '6px 12px 8px', display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusDot color={statusColor} />
        <span style={{ fontSize: 10, fontWeight: 700, color: statusColor }}>{statusLabel}</span>
        {durationStr && (
          <span style={{ fontSize: 10, color: muted, marginLeft: 'auto' }}>{durationStr}</span>
        )}
        {tokensStr && (
          <span style={{ fontSize: 10, color: muted }}>{tokensStr}</span>
        )}
      </div>
      {tools?.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, marginTop: 2 }}>
          {tools.slice(0, 5).map(tool => (
            <span key={tool} style={{
              fontSize: 9, padding: '1px 5px', borderRadius: 3,
              background: isDark ? '#1a3050' : '#cfe2f8',
              color: isDark ? '#8bb8e8' : '#0550ae',
              fontFamily: 'monospace',
            }}>
              {tool}
            </span>
          ))}
          {tools.length > 5 && (
            <span style={{ fontSize: 9, color: muted }}>+{tools.length - 5}</span>
          )}
        </div>
      )}
      {iterationCount > 0 && (
        <div style={{ fontSize: 9, color: muted, marginTop: 1 }}>
          {iterationCount} event{iterationCount !== 1 ? 's' : ''} · click to expand
        </div>
      )}
    </div>
  )
}
