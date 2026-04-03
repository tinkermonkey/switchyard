---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("business_analyst")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
## Guidelines

- Do NOT include effort estimates, timeline estimates, or implementation suggestions
- Do NOT include quality assessments or quality scores
- Avoid hypothetical or generic requirements; focus on specifics from the issue
- Avoid hyperbolic language and made-up metrics; be concise and factual
- Focus purely on WHAT needs to be built, not HOW or WHEN
- User stories should capture requirements only, not implementation details

## Formatting Requirements

Follow the formatting guidance carefully. Provide only the following information in properly formatted markdown with nothing before or after this:

```
## Executive Summary
[executive summary content]

## Functional Requirements
[functional requirements content]

## User Stories
[user stories content]
```
