---
invoked_by: scripts/generate_artifacts.py — review_generated_artifacts() via load_prompt("artifacts/review_artifacts", ...)
variables:
  project: Project name
  agent_count: Number of agent files to review
  agent_files: Pre-formatted bullet list of agent file paths
  skill_count: Number of skill files to review
  skill_files: Pre-formatted bullet list of skill file paths
---

# Agent Team Quality Review

You are reviewing the generated agent and skill definitions for **{project}**.

## Artifacts to Review

**Agents ({agent_count}):**
{agent_files}

**Skills ({skill_count}):**
{skill_files}

## Your Mission

Review ALL generated artifacts and fix any issues found. Focus on quality, accuracy, and polish.

### Review Criteria

For each artifact:

1. **Placeholder Removal** (Critical):
   - Find and fix unfilled placeholders like `{e}`, `{trace_id}`, `{store_error}`, etc.
   - Replace with realistic variable names or complete the code examples
   - Common placeholders to fix:
     - Exception variables: `{e}` → `e` or specific name
     - IDs: `{trace_id}` → `trace_id` or example value
     - Errors: `{store_error}` → `store_error`
     - Lists: `{invalid_services}` → `invalid_services`

2. **Code Example Quality**:
   - Ensure all code examples are complete and runnable
   - Use actual file paths from the project
   - Follow project conventions (async/await, type hints, etc.)
   - Remove any template artifacts or incomplete snippets

3. **Consistency**:
   - Verify YAML frontmatter is complete and valid
   - Check that tone and formatting are consistent across all artifacts
   - Ensure descriptions are clear and actionable

4. **Accuracy**:
   - Verify file paths and line numbers are correct (use Read/Grep to check)
   - Confirm port interfaces and method signatures match actual code
   - Test that example commands will actually work

5. **Optimization**:
   - Remove redundancy and verbosity
   - Clarify ambiguous instructions
   - Add missing context where needed
   - Improve examples to be more practical

6. **Formatting**:
   - Ensure proper markdown formatting
   - Code blocks have correct language tags
   - Lists and sections are well-structured

## Process

1. Read each artifact file using the Read tool
2. Identify issues based on criteria above
3. Use the Edit tool to fix issues (preserve existing content structure)
4. Focus on surgical edits - don't rewrite unnecessarily
5. After reviewing all files, provide a summary of changes made

## Important Guidelines

- Make targeted fixes, not wholesale rewrites
- Preserve the core content and knowledge base
- Use Edit tool for changes (not Write - we want to preserve content)
- If you find a file path reference, verify it exists before keeping it
- If unsure about something, leave it rather than guessing

## Expected Output

After reviewing and fixing all artifacts, provide a summary:

```markdown
# Quality Review Summary

## Changes Made

### Agents
- agent-name.md: Fixed X placeholders, improved Y examples
- ...

### Skills
- skill-name/SKILL.md: Fixed X issues, clarified Y
- ...

## Statistics
- Total artifacts reviewed: X
- Issues found: Y
- Issues fixed: Z
- Artifacts modified: N

## Validation Ready
All artifacts should now pass validation without warnings.
```
