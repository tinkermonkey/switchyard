---
invoked_by: prompts/builder.py — PromptBuilder._output_instructions() via loader.workflow_template("question/output_code")
  Used when mode == "question" and is_file_writer=True (ctx.makes_code_changes or ctx.filesystem_write_allowed)
variables: none
---

**IMPORTANT — OUTPUT FORMAT**:
- **PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first.
- Use proper markdown formatting (headers, lists, code blocks)
- **NO INTERNAL DIALOG**: Do not include planning statements like "Let me research...", "I'll examine...". Just provide the answer.
- You may create, edit, or modify files if requested
- Your changes will be auto-committed to git
- Provide a summary of your work/answer as the GitHub comment
