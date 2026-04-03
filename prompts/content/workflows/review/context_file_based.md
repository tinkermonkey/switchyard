---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_context_section() when rc and pipeline_context_dir are set
variables:
  maker_file: Filename of the maker's current output (e.g. maker_output_1.md)
  prev_feedback_note: Optional bullet line referencing prior feedback file (with trailing newline), or empty string
---
## Review Cycle Context Files

All context files are at `/pipeline_context/`:
- **`current_diff.md`** — git changes to review (stat + commits) ← run `git diff` from those commits
- **`{maker_file}`** — current implementation to review
- `initial_request.md` — original requirements to verify against
{prev_feedback_note}- Earlier numbered files show the full iteration history

**Review focus**: Read `current_diff.md` for the list of changed files, then use
`git diff <base_commit> HEAD -- <file>` to examine the actual changes. Review ONLY
additions (`+`) and deletions (`-`). Do not review unchanged code.
