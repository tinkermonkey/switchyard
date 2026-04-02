---
invoked_by: prompts/builder.py — PromptBuilder.build_verifier_prompt() via loader.workflow_template("verification/prompt")
variables:
  project_name: Project name string; resolved from ctx.project_name or ctx.project
  iteration_context: Pre-formatted review cycle context block from _verifier_iteration_context();
    describes initial vs re-verify pass, iteration count, and previous feedback if re-verifying;
    empty string if no review_cycle on context
  issue_title: GitHub issue title
  issue_body: GitHub issue body / description
  previous_stage: Output from the dev_environment_setup agent (ctx.previous_stage); the setup
    work that is being verified
  verification_task: Content of agents/dev_environment_verifier/review_task.md with all
    {project_name} placeholders pre-expanded to the actual project name
---
You are verifying the development environment setup for project: **{project_name}**

{iteration_context}

Original Issue:
Title: {issue_title}
Description: {issue_body}

Dev Environment Setup Agent's Output:
{previous_stage}

{verification_task}
