---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_context_section() when rc and pipeline_context_dir are set
variables:
  maker_file: Filename of the maker's current output (e.g. maker_output_1.md)
  prev_feedback_note: Optional bullet line referencing prior feedback file (with trailing newline), or empty string
---
## Review Cycle Context Files

All context files are at `/pipeline_context/`:
- **`{maker_file}`** — what the maker implemented (primary source of truth)
- **`current_diff.md`** — git changes since before the maker ran; use this to find the relevant files quickly
- `initial_request.md` — original requirements to verify against
{prev_feedback_note}- Earlier numbered files show the full iteration history

**Review focus**: Read `{maker_file}` to understand what was implemented, then use
`current_diff.md` to locate the changed files and run `git diff <base_commit> HEAD -- <file>`
to examine the actual code. If the diff is empty — because the maker recovered without
making new changes, or the snapshot is stale — locate the relevant code via `{maker_file}`
and `git log` instead. Review the implementation as it exists, not just the delta.
