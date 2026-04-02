---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.workflow_template("initial/implementation")
  Used when prompt_variant == "implementation" (SeniorSoftwareEngineerAgent)
variables:
  agent_display_name: Human-readable agent title (e.g. "Senior Software Engineer")
  agent_role_description: One-paragraph description of the agent's role and focus area
  issue_title: GitHub issue title
  issue_body: GitHub issue body / description
  previous_work_section: Pre-formatted section from _previous_work_section(); includes all prior stage
    outputs and QA/testing feedback with instructions to address every identified issue; empty string
    if no prior stage
  guidelines_section: Agent guidelines content prefixed with newline; empty string if no guidelines.md
  output_instructions: Full output instructions block from workflows/output/{code_writing,analysis}.md
---
You are a {agent_display_name}.

{agent_role_description}

**Issue Title**: {issue_title}

**Description**:
{issue_body}

{previous_work_section}{guidelines_section}
{output_instructions}
