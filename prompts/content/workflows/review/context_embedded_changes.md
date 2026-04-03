---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_context_section() fallback when change_manifest is set
variables:
  change_manifest: The change manifest listing modified files with git diff commands
---
## Code Changes

{change_manifest}

**Review focus**: Use the `git diff` commands listed above to fetch and examine the actual
changes before reviewing. Review ONLY additions (`+`) and deletions (`-`) in the diff output.
Do not review unchanged code.
