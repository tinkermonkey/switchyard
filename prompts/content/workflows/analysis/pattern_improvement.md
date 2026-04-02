---
invoked_by: services/pattern_llm_analyzer.py — _build_pattern_analysis_prompt() via default_loader.workflow_template("analysis/pattern_improvement")
  Called as: loader.workflow_template("analysis/pattern_improvement").format(
    pattern_name=..., occurrence_count=..., project_count=..., projects_affected=...,
    agents_affected=..., severity=..., category=..., avg_impact=..., total_time_wasted=..., examples_text=...)
variables:
  pattern_name: Pattern type/name from pattern detection
  occurrence_count: Number of times this pattern occurred
  project_count: Number of distinct projects affected
  projects_affected: Comma-separated list of affected projects (up to 5)
  agents_affected: Comma-separated list of affected agents (up to 3)
  severity: Pattern severity level
  category: Pattern category
  avg_impact: Average impact in seconds per occurrence
  total_time_wasted: Total seconds wasted across all occurrences
  examples_text: Pre-formatted bullet list of example instances
---

You are analyzing agent behavior logs to improve CLAUDE.md instructions that guide AI agents.

## Pattern Summary
**Type:** {pattern_name}
**Frequency:** {occurrence_count} occurrences across {project_count} projects
**Projects affected:** {projects_affected}
**Agents affected:** {agents_affected}
**Severity:** {severity}
**Category:** {category}
**Average impact:** ~{avg_impact} seconds per occurrence
**Total time wasted:** ~{total_time_wasted} seconds

## Example Instances
{examples_text}

## Task
Propose a specific, concise addition or modification to CLAUDE.md that would prevent this pattern. Follow these constraints:

1. **Be specific and actionable** - Not philosophical or general advice
2. **Use concrete examples** - Show exact commands or patterns to use/avoid
3. **Keep it concise** - Under 150 words
4. **Format as a git diff** - Show exactly what to add/change
5. **Specify the section** - Which part of CLAUDE.md (e.g., "Git Operations", "File System Safety", "Best Practices")

## Output Format

Return your response in this exact format:

### SECTION
<section_name>

### PROPOSED_CHANGE
```diff
<git diff format showing addition or change>
```

### EXPECTED_IMPACT
<1-2 sentences on how this prevents the pattern>

### REASONING
<2-3 sentences explaining why this pattern occurs and why your fix helps>

Be direct and technical. Focus on preventing the specific error pattern.
