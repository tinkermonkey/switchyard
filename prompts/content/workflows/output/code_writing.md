---
invoked_by: prompts/builder.py — PromptBuilder._output_instructions() via loader.workflow_template("output/code_writing")
  Used when mode != "question" and is_file_writer=True (ctx.makes_code_changes or ctx.filesystem_write_allowed)
variables: none
---

**IMPORTANT**:
- Provide a short summary of your work as the final output
- Use proper markdown formatting (headers, lists, code blocks)
- You may create, edit, or modify files as needed to complete your task
- Your changes will be auto-committed to git

**CRITICAL — MARK YOUR FINAL OUTPUT**: Wrap your summary between these exact markers, as the last thing you write:
`<<<FINAL_OUTPUT>>>`
...your short summary...
`<<<END_FINAL_OUTPUT>>>`
Only the content between these markers is posted to GitHub — everything outside them is discarded. If you produce a revised summary in a later turn (for example, after a background task you started finishes), wrap that one too; only the last marked block is used.
