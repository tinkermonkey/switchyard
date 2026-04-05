---
invoked_by: prompts/builder.py — PromptBuilder._reference_repos_section()
  Appended to every agent prompt when the project has reference_repos configured.
variables:
  entries: Newline-joined list of "- **/reference/<name>** — <description>" lines
---
## Reference Repositories

The following repositories are available to you **read-only** inside this container:

{entries}

These repos are mounted at the listed paths. You can read any file in them freely. Do not attempt to write to them.
