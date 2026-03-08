/**
 * Single-pass extractor for claude_log events.
 *
 * Scans `allEvents` (the full unfiltered event array, including claude_log) and
 * returns a Map<task_id, { inputTokens, outputTokens, tools[] }>.
 *
 * Event schema (top-level fields on each claude_log event):
 *   event_type === 'tool_call'   → token_effective_input (cumulative), token_output (per-call), tool_name
 *   event_type === 'text_output' → token_effective_input (cumulative), token_output (per-call)
 *
 * Token strategy:
 *   inputTokens  = max(token_effective_input) across all events for the task.
 *                  token_effective_input is the total context window used at each step
 *                  (new tokens + cache hits), so the max is the peak context consumed.
 *   outputTokens = sum(token_output) across all events — each value is the output
 *                  tokens generated in that individual API call.
 *
 * Tool strategy:
 *   Collect unique tool_name values from tool_call events, sorted alphabetically.
 *   Excludes 'TodoWrite' which is internal bookkeeping, not a meaningful tool call.
 *
 * Graceful degradation: returns an empty Map if allEvents is null/empty or contains
 * no claude_log entries, so callers never need to null-check the return value.
 */

const EXCLUDED_TOOLS = new Set(['TodoWrite'])

export function extractClaudeLogSummaries(allEvents) {
  if (!allEvents?.length) return new Map()

  const raw = new Map()

  for (const event of allEvents) {
    if (event.event_category !== 'claude_log') continue
    const taskId = event.task_id
    if (!taskId) continue

    let entry = raw.get(taskId)
    if (!entry) {
      entry = { maxEffectiveInput: 0, outputTokens: 0, tools: new Set() }
      raw.set(taskId, entry)
    }

    const effectiveInput = event.token_effective_input
    if (effectiveInput != null) {
      entry.maxEffectiveInput = Math.max(entry.maxEffectiveInput, effectiveInput)
    }

    const outputTokens = event.token_output
    if (outputTokens != null) {
      entry.outputTokens += outputTokens
    }

    if (event.event_type === 'tool_call' && event.tool_name && !EXCLUDED_TOOLS.has(event.tool_name)) {
      entry.tools.add(event.tool_name)
    }
  }

  const result = new Map()
  for (const [k, v] of raw) {
    result.set(k, {
      inputTokens: v.maxEffectiveInput,
      outputTokens: v.outputTokens,
      tools: [...v.tools].sort(),
    })
  }
  return result
}
