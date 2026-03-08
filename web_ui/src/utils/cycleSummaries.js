/**
 * Pure data extractors for cycle container collapsed summaries.
 * All functions are null-safe — missing data returns null fields, never throws.
 * No JSX — pure JavaScript only.
 */

export function extractRepairCycleSummary(cycle) {
  try {
    const events = cycle.events ?? []
    const completedEvent = events.find(e => e.event_type === 'repair_cycle_completed')

    let status = 'running'
    if (cycle.endEvent && !cycle.endEvent._inferred) {
      if (cycle.endEvent.event_type === 'agent_failed') {
        status = 'failed'
      } else if (completedEvent != null) {
        status = completedEvent.overall_success === false ? 'failed' : 'success'
      } else {
        status = 'success'
      }
    }

    return {
      status,
      durationSeconds: completedEvent?.duration_seconds ?? null,
      totalAgentCalls: completedEvent?.total_agent_calls ?? null,
      envRebuildTriggered: events.some(e => e.event_type === 'repair_cycle_env_rebuild_started'),
      testCycleRows: (cycle.testCycles ?? []).map(tc => ({
        testType: tc.testType,
        passed: tc.endEvent?.passed ?? null,
        filesFixed: tc.endEvent?.files_fixed ?? null,
        iterations: tc.endEvent?.test_cycle_iterations ?? null,
        durationSeconds: tc.endEvent?.duration_seconds ?? null,
      })),
    }
  } catch {
    return { status: 'running', durationSeconds: null, totalAgentCalls: null, envRebuildTriggered: false, testCycleRows: [] }
  }
}

export function extractTestCycleSummary(tc) {
  try {
    // Try tc.events first for repair_cycle_test_execution_completed, then sub-cycle endEvents
    const fromEvents = (tc.events ?? []).filter(e => e.event_type === 'repair_cycle_test_execution_completed')
    const fromSubCycles = (tc.subCycles ?? [])
      .filter(sc => sc.cycleType === 'test_execution' && sc.endEvent)
      .map(sc => sc.endEvent)
    const lastTestExec = [...fromEvents, ...fromSubCycles].pop() ?? null

    let testResultRow = null
    if (lastTestExec) {
      const pc = lastTestExec.passed ?? lastTestExec.passed_count ?? null
      const fc = lastTestExec.failed ?? lastTestExec.failed_count ?? null
      const wc = lastTestExec.warnings ?? lastTestExec.warning_count ?? null
      if (pc != null || fc != null) {
        testResultRow = { passedCount: pc, failedCount: fc, warningsCount: wc }
      }
    }

    return {
      testType: tc.testType,
      passed: tc.endEvent?.passed ?? null,
      filesFixed: tc.endEvent?.files_fixed ?? null,
      iterationsUsed: tc.endEvent?.test_cycle_iterations ?? null,
      durationSeconds: tc.endEvent?.duration_seconds ?? null,
      warningsReviewed: tc.endEvent?.warnings_reviewed ?? null,
      testResultRow,
      hadSystemicFix: (tc.subCycles ?? []).some(sc => sc.cycleType === 'systemic_fix'),
    }
  } catch {
    return {
      testType: tc?.testType ?? '',
      passed: null,
      filesFixed: null,
      iterationsUsed: null,
      durationSeconds: null,
      warningsReviewed: null,
      testResultRow: null,
      hadSystemicFix: false,
    }
  }
}

export function extractSubCycleSummary(sc) {
  try {
    const endEvent = sc.endEvent
    const outcome = endEvent && !endEvent._inferred ? 'complete' : 'running'

    return {
      cycleType: sc.cycleType,
      outcome,
      filesFixed: endEvent?.files_fixed ?? null,
      warningCount: endEvent?.warnings_reviewed ?? endEvent?.warning_count ?? null,
      patternCategory: endEvent?.pattern_category ?? null,
      affectedFiles: endEvent?.affected_files_count ?? null,
      testPassedCount: endEvent?.passed ?? endEvent?.passed_count ?? null,
      testFailedCount: endEvent?.failed ?? endEvent?.failed_count ?? null,
    }
  } catch {
    return {
      cycleType: sc?.cycleType ?? '',
      outcome: null,
      filesFixed: null,
      warningCount: null,
      patternCategory: null,
      affectedFiles: null,
      testPassedCount: null,
      testFailedCount: null,
    }
  }
}

export function extractReviewCycleSummary(cycle) {
  try {
    const endEvent = cycle.endEvent
    let status = 'running'
    if (endEvent && !endEvent._inferred) {
      const outcome = endEvent.outcome ?? endEvent.decision?.outcome
      if (outcome === 'approved') status = 'approved'
      else if (outcome === 'rejected') status = 'rejected'
      else if (outcome === 'escalated') status = 'escalated'
      else status = 'approved'
    }

    let durationSeconds = null
    if (cycle.startEvent?.timestamp && endEvent?.timestamp && !endEvent._inferred) {
      const startMs = new Date(cycle.startEvent.timestamp).getTime()
      const endMs   = new Date(endEvent.timestamp).getTime()
      if (!isNaN(startMs) && !isNaN(endMs) && endMs > startMs)
        durationSeconds = Math.round((endMs - startMs) / 1000)
    }

    const iterations = (cycle.iterations ?? []).map((iter, idx) => {
      const startMs = new Date(iter.startEvent?.timestamp).getTime()
      const nextIter = cycle.iterations[idx + 1]
      const endMs = nextIter
        ? new Date(nextIter.startEvent?.timestamp).getTime()
        : endEvent ? new Date(endEvent.timestamp).getTime() : NaN
      const durSec = (!isNaN(startMs) && !isNaN(endMs) && endMs > startMs)
        ? Math.round((endMs - startMs) / 1000)
        : null
      return { number: iter.number ?? idx + 1, durationSeconds: durSec }
    })

    const completionReason = (endEvent && !endEvent._inferred) ? (endEvent.reason ?? null) : null

    return {
      status,
      makerAgent: cycle.startEvent?.inputs?.maker_agent ?? cycle.startEvent?.maker_agent ?? null,
      reviewerAgent: cycle.startEvent?.inputs?.reviewer_agent ?? cycle.startEvent?.reviewer_agent ?? null,
      totalIterations: cycle.iterations?.length ?? 0,
      maxIterations: cycle.startEvent?.max_iterations ?? null,
      durationSeconds,
      iterations,
      completionReason,
    }
  } catch {
    return { status: 'running', makerAgent: null, reviewerAgent: null, totalIterations: 0, maxIterations: null, durationSeconds: null, iterations: [], completionReason: null }
  }
}

export function extractReviewIterationSummary(iteration, cycleStartEvent) {
  try {
    return {
      makerAgent: cycleStartEvent?.inputs?.maker_agent ?? cycleStartEvent?.maker_agent ?? null,
      reviewerAgent: cycleStartEvent?.inputs?.reviewer_agent ?? cycleStartEvent?.reviewer_agent ?? null,
      eventCount: iteration.events?.length ?? 0,
    }
  } catch {
    return { makerAgent: null, reviewerAgent: null, eventCount: 0 }
  }
}

export function extractPRReviewCycleSummary(cycle) {
  try {
    const endEvent = cycle.endEvent
    let status = 'running'
    if (endEvent && !endEvent._inferred) {
      status = endEvent.status === 'failed' ? 'failed' : 'completed'
    }

    return {
      status,
      phaseCount: cycle.phases?.length ?? 0,
      finalStatus: endEvent?.status ?? null,
    }
  } catch {
    return { status: 'running', phaseCount: 0, finalStatus: null }
  }
}

export function extractPRReviewPhaseSummary(phase) {
  try {
    return {
      phaseNumber: phase.number,
      eventCount: phase.events?.length ?? 0,
    }
  } catch {
    return { phaseNumber: 0, eventCount: 0 }
  }
}

export function extractConversationalLoopSummary(cycle) {
  try {
    const endEvent = cycle.endEvent
    const status = endEvent && !endEvent._inferred ? 'paused' : 'running'
    const exchangeCount = (cycle.events ?? []).filter(e => e.event_type === 'agent_initialized').length

    let durationSeconds = null
    if (cycle.startEvent?.timestamp && endEvent?.timestamp && !endEvent._inferred) {
      const startMs = new Date(cycle.startEvent.timestamp).getTime()
      const endMs = new Date(endEvent.timestamp).getTime()
      if (!isNaN(startMs) && !isNaN(endMs) && endMs > startMs) {
        durationSeconds = Math.round((endMs - startMs) / 1000)
      }
    }

    return { status, exchangeCount, durationSeconds }
  } catch {
    return { status: 'running', exchangeCount: 0, durationSeconds: null }
  }
}
