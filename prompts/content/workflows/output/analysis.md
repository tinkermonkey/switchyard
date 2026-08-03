---
invoked_by: prompts/builder.py — PromptBuilder._output_instructions() via loader.workflow_template("output/analysis")
  Used when mode != "question" and is_file_writer=False (ctx.makes_code_changes and ctx.filesystem_write_allowed are both False)
variables: none
---

**IMPORTANT — OUTPUT FORMAT FOR ANALYSIS**:
- Output your analysis as markdown text directly in your response
- DO NOT create any files — this will be posted to GitHub as a comment
- **NO SUMMARY SECTIONS**: Do NOT create a "Summary for GitHub Comment" section at the end — your entire output IS the comment
- Focus on WHAT needs to be done, not HOW or WHEN
- Be specific and factual, avoid hypotheticals and hyperbole
- Use proper markdown formatting (headers, lists, code blocks)

**CRITICAL — MARK YOUR FINAL OUTPUT**: Wrap your complete analysis between these exact markers, as the last thing you write:
`<<<FINAL_OUTPUT>>>`
...your full markdown analysis...
`<<<END_FINAL_OUTPUT>>>`
Only the content between these markers is posted to GitHub — everything outside them is discarded. If you produce a revised or corrected version in a later turn (for example, after a background task you started finishes), wrap that one too; only the last marked block is used. Do not end your turn with an unmarked status remark like "the analysis above is complete" — that remark, not the analysis, is what would be posted if you forget the markers.
