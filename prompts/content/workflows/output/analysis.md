---
invoked_by: prompts/builder.py — PromptBuilder._output_instructions() via loader.workflow_template("output/analysis")
  Used when mode != "question" and is_file_writer=False (ctx.makes_code_changes and ctx.filesystem_write_allowed are both False)
variables: none
---

**IMPORTANT — OUTPUT FORMAT FOR ANALYSIS**:
- **PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first. The project's CLAUDE.md file defines project-specific conventions and documentation requirements that take precedence over these general instructions.
- Output your analysis as markdown text directly in your response
- DO NOT create any files — this will be posted to GitHub as a comment
- DO NOT include project name, feature name, or date headers (this info is already in the discussion)
- **START IMMEDIATELY** with your first section heading (e.g., "## Executive Summary" or "## Problem Abstraction")
- **NO CONVERSATIONAL PREAMBLES**: Do NOT include statements like "Ok, I'll build...", "I'll analyze...", "Let me create...", etc.
- **NO SUMMARY SECTIONS**: Do NOT create a "Summary for GitHub Comment" section at the end — your entire output IS the comment
- **NO INTERNAL DIALOG**: Do NOT include planning statements like "Let me research...", "I'll examine...", "Now let me check..."
- **NO TOOL USAGE COMMENTARY**: Do not narrate what tools you're using or what you're searching for
- Focus on WHAT needs to be done, not HOW or WHEN
- Be specific and factual, avoid hypotheticals and hyperbole
- Use proper markdown formatting (headers, lists, code blocks)
