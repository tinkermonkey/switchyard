/**
 * Single-pass extractor for claude_log streaming events.
 *
 * Scans `allEvents` (the full unfiltered event array, including claude_log) and
 * returns a Map<task_id, { inputTokens, outputTokens, tools[] }> that agent nodes
 * can use to render token counts and tool pills.
 *
 * Parsing rules:
 *   raw_event.type === 'message_start'        → accumulate input_tokens
 *   raw_event.type === 'message_delta'        → accumulate output_tokens
 *   raw_event.type === 'content_block_start'
 *     && content_block.type === 'tool_use'    → collect tool name
 *
 * Graceful degradation: returns an empty Map if allEvents is null/empty or contains
 * no claude_log entries, so callers never need to null-check the return value.
 */
export function extractClaudeLogSummaries(allEvents) {
  if (!allEvents?.length) return new Map()

  const raw = new Map()

  for (const event of allEvents) {
    if (event.event_category !== 'claude_log') continue
    const taskId = event.task_id
    if (!taskId) continue
    const re = event.raw_event
    if (!re) continue

    let entry = raw.get(taskId)
    if (!entry) {
      entry = { inputTokens: 0, outputTokens: 0, tools: new Set() }
      raw.set(taskId, entry)
    }

    if (re.type === 'message_start' && re.message?.usage) {
      entry.inputTokens += re.message.usage.input_tokens ?? 0
    } else if (re.type === 'message_delta' && re.usage) {
      entry.outputTokens += re.usage.output_tokens ?? 0
    } else if (
      re.type === 'content_block_start' &&
      re.content_block?.type === 'tool_use' &&
      re.content_block?.name
    ) {
      entry.tools.add(re.content_block.name)
    }
  }

  const result = new Map()
  for (const [k, v] of raw) {
    result.set(k, {
      inputTokens: v.inputTokens,
      outputTokens: v.outputTokens,
      tools: [...v.tools].sort(),
    })
  }
  return result
}
