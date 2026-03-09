/**
 * Terminal event definitions for each event-driven cycle type.
 *
 * terminalEvents: maps event_type → { status?: string, isFailure: bool }
 *   - status: fixed status string to assign when this event closes the cycle.
 *             Omitted when status must be derived from the event's outcome field.
 *   - isFailure: true for abnormal/failure terminal events.
 *
 * Drives two concerns:
 *   1. Boundary detection (eventProcessing/index.js) — which events close a cycle.
 *   2. Status extraction (cycleSummaries.js) — what status to assign the closed cycle.
 *
 * Note: repair_cycle uses agent_lifecycle events (agent_initialized/agent_completed/
 * agent_failed), not decision events, so it follows a different pattern and is
 * not listed here.
 */
export const CYCLE_TERMINAL_EVENTS = {
  review_cycle: {
    startEvent: 'review_cycle_started',
    terminalEvents: {
      'review_cycle_completed': { isFailure: false },           // status derived from outcome field
      'review_cycle_escalated': { isFailure: true, status: 'escalated' },
    },
    syntheticCloseType: 'review_cycle_completed',
  },
  status_progression: {
    startEvent: 'status_progression_started',
    terminalEvents: {
      'status_progression_completed': { isFailure: false },
      'status_progression_failed':    { isFailure: true, status: 'failed' },
    },
    syntheticCloseType: 'status_progression_completed',
  },
  pr_review_cycle: {
    startEvent: 'pr_review_stage_started',
    terminalEvents: {
      'pr_review_stage_completed': { isFailure: false },
    },
    syntheticCloseType: 'pr_review_stage_completed',
  },
  conversational_loop: {
    startEvents: ['conversational_loop_started', 'conversational_loop_resumed'],
    terminalEvents: {
      'conversational_loop_paused': { isFailure: false },
    },
    syntheticCloseType: 'conversational_loop_paused',
  },
}
