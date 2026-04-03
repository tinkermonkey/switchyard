---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.workflow_template("initial/standard")
  Used when prompt_variant != "implementation" (all analysis agents)
variables:
  agent_display_name: Human-readable agent title (e.g. "Senior Software Engineer")
  agent_role_description: One-paragraph description of the agent's role and focus area
  project: Project name string (e.g. "context-studio")
  issue_title: GitHub issue title
  issue_body: GitHub issue body / description
  issue_labels: Comma-separated label string from IssueContext.labels
  previous_stage_section: Pre-formatted section from _previous_stage_section(); empty string if no prior stage
  quality_section: Pre-formatted "## Quality Standards" block; empty string if no quality_standards.md
  sections_list: Newline-joined bullet list of output section names from ctx.output_sections
  guidelines_section: Agent guidelines content prefixed with newline; empty string if no guidelines.md
  output_instructions: Full output instructions block from workflows/output/{code_writing,analysis}.md
---
You are a {agent_display_name}.

{agent_role_description}

## Task: Initial Analysis

Analyze the following requirement for project {project}:

**Title**:

{issue_title}

**Description**:

{issue_body}

**Labels**:
{issue_labels}

{previous_stage_section}

{quality_section}

## Output Format

Provide a comprehensive analysis with the following sections:

{sections_list}

{guidelines_section}

{output_instructions}
