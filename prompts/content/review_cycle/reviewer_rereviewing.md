
## Review Cycle Context — Re-Review Mode

This is **Re-Review Iteration {iteration} of {max_iterations}**.

**Maker Agent**: {maker_agent_title} has revised their work based on your previous feedback.

{prior_feedback_section}**IMPORTANT — Review Scope**:
- You are reviewing ONLY the changes made by {maker_agent_title} in response to feedback
- DO NOT re-review work that was previously approved by other review cycles
- Focus on the commits made by {maker_agent_title} since the last review

**Your Task**: Verify previous issues are resolved. Be concise.

**Review Approach**:
1. Check if your PREVIOUS feedback items (listed above) were addressed (don't re-raise if fixed)
2. Note any NEW issues discovered in the maker's changes
3. Make your decision

**Keep Feedback CONCISE**:
- 1–2 sentences per issue maximum
- Focus on WHAT is wrong, not explaining WHY it's important (developer already knows)
- Only include items that genuinely need fixing
- Don't repeat issues that were already addressed

**Common Issues**:
- Adding capabilities that were not requested → these need to be removed
- Markdown files with developer notes and implementation details → these need to be removed
- Test and debug scripts that are not in the test folder tree → these need to be evaluated and cleaned up
- Leaving in or commenting out code that was meant to be replaced or removed → these need to be cleaned up
- Code with names including "Phase X" or "Step Y" → these need to be renamed to meaningful names

**Escalation**: After {max_iterations} iterations, unresolved issues will escalate to human review.
