/**
 * Terminal event definitions for every cycle type recognised by the UI.
 *
 * Each entry describes how to detect cycle boundaries:
 *
 *   startEvent / startEvents  - event_type(s) that open a cycle
 *   terminalEvents            - map of event_type → { status?: string, isFailure: bool }
 *     - status: fixed status string to assign when this event closes the cycle.
 *               Omitted when status must be derived from the event's outcome field.
 *     - isFailure: true for abnormal/failure terminal events.
 *   syntheticCloseType        - event_type used when inferring a close for an open cycle
 *
 * Optional fields for non-decision cycle types:
 *
 *   eventCategory             - event_category to match (default: 'decision')
 *   matchFields               - { field: value } pairs that must ALL match on the event
 *   excludeFields             - { field: value } pairs where ANY match excludes the event
 *   syntheticCloseOverrides   - extra fields merged into synthetic close events
 *   pairByField               - when set, pairs start/end by matching this field value
 *                                instead of sequential-window pairing (for concurrent cycles)
 *
 * Drives two concerns:
 *   1. Boundary detection (eventProcessing/index.js) — which events open/close a cycle.
 *   2. Status extraction (cycleSummaries.js) — what status to assign the closed cycle.
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
  repair_cycle: {
    eventCategory: 'agent_lifecycle',
    startEvent: 'agent_initialized',
    matchFields: { agent: 'repair_cycle' },
    terminalEvents: {
      'agent_completed': { isFailure: false },
      'agent_failed':    { isFailure: true, status: 'failed' },
    },
    syntheticCloseType: 'agent_completed',
    syntheticCloseOverrides: { event_category: 'agent_lifecycle', agent: 'repair_cycle', success: null, error: null, duration_ms: null },
  },
  agent_execution: {
    eventCategory: 'agent_lifecycle',
    startEvent: 'agent_initialized',
    excludeFields: { agent: 'repair_cycle' },
    terminalEvents: {
      'agent_completed': { isFailure: false },
      'agent_failed':    { isFailure: true, status: 'failed' },
    },
    syntheticCloseType: 'agent_completed',
    syntheticCloseOverrides: { event_category: 'agent_lifecycle', success: null, error: null, duration_ms: null },
    pairByField: 'task_id',
  },
}
