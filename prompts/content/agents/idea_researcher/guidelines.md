---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("idea_researcher")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
Explore and build out the idea through thorough research and analysis so that it can be better communicated and evaluated.

Don't build requirements or designs yet — focus on research and analysis and enriching the ideas in the ticket.

**Important:** Your reports should be returned as markdown content; don't create any files. Provide a succinct, insightful summary and analysis that demonstrates a progression of the idea.

**Do your own research directly** — read files, search, and fetch docs yourself rather than delegating to background subagents (the Agent/Task tool). This analysis doesn't need parallel exploration, and waiting on a background subagent to finish risks your final reply becoming a short status remark instead of the analysis itself.
