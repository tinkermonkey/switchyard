---
invoked_by: pipeline/repair_cycle.py — _review_warnings() via default_loader.workflow_template("repair/warning_review")
  Called as: loader.workflow_template("repair/warning_review").format(source_file=source_file, warning_text=warning_text)
variables:
  source_file: The source file that produced the warnings
  warning_text: The formatted warning output text
---

Review these warnings from a run of {source_file}:

{warning_text}

For each warning:
- Determine if it's expected in this context
- If not expected, fix the underlying issue
