---
invoked_by: services that perform pattern improvement analysis
variables:
  pattern_name: Identifier for the recurring failure pattern
  occurrence_count: Total number of occurrences observed
  project_count: Number of distinct projects affected
  projects_affected: Comma-separated list of affected project names
  agents_affected: Comma-separated list of affected agent names
  severity: Severity level of the pattern (high/medium/low)
  category: Category of the pattern (e.g. error_handling, testing, naming)
  avg_impact: Average impact score per occurrence
  total_time_wasted: Cumulative time wasted due to this pattern (hours)
  examples_text: Pre-formatted examples of the pattern occurring
---
## Pattern Improvement Recommendation

**Pattern**: `{pattern_name}`
**Severity**: {severity} | **Category**: {category}
**Occurrences**: {occurrence_count} across {project_count} project(s)

### Impact

- **Projects Affected**: {projects_affected}
- **Agents Affected**: {agents_affected}
- **Average Impact per Occurrence**: {avg_impact} hours
- **Total Time Wasted**: {total_time_wasted} hours

### Examples

{examples_text}

### Recommended Fix

Analyze the examples above and produce a recommendation that is **specific and actionable** — identify
the root cause, propose a concrete change (e.g. to agent guidelines, prompt templates, or tool usage),
and describe the expected improvement.

Do not give generic advice. Your recommendation must directly address the specific pattern `{pattern_name}`
as shown in the examples.
