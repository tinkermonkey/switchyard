---
invoked_by: services that perform ignored review pattern analysis
variables:
  agent: Agent name that is ignoring this review pattern
  category: Category of the ignored review pattern (e.g. style, testing, naming)
  severity: Severity level of the pattern (high/medium/low)
  ignore_rate: Percentage of occurrences where this pattern was ignored (e.g. "62.5%")
  sample_size: Number of review instances analysed
  examples_text: Pre-formatted examples of the ignored pattern
---
## Ignored Review Pattern Analysis

**Agent**: `{agent}`
**Category**: {category} | **Severity**: {severity}
**Ignore Rate**: {ignore_rate} (across {sample_size} samples)

### Examples of Ignored Feedback

{examples_text}

### Analysis Task

The `{agent}` agent is ignoring {category} review feedback at a rate of {ignore_rate}.

Review the examples above and determine:

1. Why is this pattern being ignored? (Unclear instructions, conflicting guidance, too noisy?)
2. Is this pattern worth enforcing? If so, what change would make `{agent}` reliably act on it?
3. If the pattern should NOT be enforced, recommend removing it from the review criteria.

Produce a specific recommendation for improving or removing this review pattern.
