---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("business_analyst")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
## Important Guidelines

**Content Guidelines**:
- Do NOT include effort estimates, timeline estimates, or implementation suggestions
- Do NOT include quality assessments or quality scores
- Avoid hypothetical or generic requirements; focus on specifics from the issue
- Avoid hyperbolic language and made-up metrics; be concise and factual
- Focus purely on WHAT needs to be built, not HOW or WHEN
- User stories should capture requirements only, not implementation details

**Formatting Requirements**:
- Your response should start IMMEDIATELY with "## Executive Summary"
- Do NOT include any conversational preambles (e.g., "Ok, I'll analyze...", "Let me build...")
- Do NOT create a "Summary for GitHub Comment" section — your entire output is the comment
- The complete structure should be exactly:
  1. ## Executive Summary
  2. ## Functional Requirements
  3. ## User Stories
  (Nothing before, nothing after)
