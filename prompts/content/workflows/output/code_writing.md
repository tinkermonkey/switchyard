---
invoked_by: prompts/builder.py — PromptBuilder._output_instructions() via loader.workflow_template("output/code_writing")
  Used when mode != "question" and is_file_writer=True (ctx.makes_code_changes or ctx.filesystem_write_allowed)
variables: none
---

**IMPORTANT**:
- **PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first. The project's CLAUDE.md file defines project-specific conventions, file organisation, and documentation requirements that take precedence over these general instructions.
- You may create, edit, or modify files as needed to complete your task
- Use the Write, Edit, and other file manipulation tools
- Your changes will be auto-committed to git
- Also provide a summary of your work as markdown for the GitHub comment
- Use proper markdown formatting (headers, lists, code blocks)
- **DO NOT include internal planning dialog**: Do not include statements like "Let me research...", "I'll examine...", "Now let me check...", etc. in your GitHub comment summary. Only include the final summary of what you did.
