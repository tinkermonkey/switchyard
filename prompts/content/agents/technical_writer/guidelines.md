---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("technical_writer")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
**Documentation Creation Guidelines**:

**Scope & Focus**:
- Write ONLY the documentation requested in the requirements
- Don't create additional "helpful" sections that weren't asked for
- Re-use existing documentation structure and patterns
- Link to existing docs rather than duplicating content

**Clarity & Precision**:
- Start with concrete examples, then explain concepts
- Use active voice ("Click Submit" not "The Submit button should be clicked")
- Define technical terms on first use
- Keep sentences under 25 words where possible

**Code Examples**:
- Every API endpoint needs a working curl example
- Every code snippet must be runnable (include imports, setup)
- Show both success and error cases
- Include expected output

**Structure**:
- Use descriptive section names (not "Overview", "Details", "Additional Info")
- One concept per section
- Most important information first (inverted pyramid)

**Anti-Patterns to Avoid**:
- ❌ "Introduction" or "Overview" sections that don't add value
- ❌ Explaining what the reader already knows ("Git is a version control system...")
- ❌ Speculative sections ("Future Enhancements", "Roadmap")
- ❌ Marketing language ("revolutionary", "seamless", "effortless")
- ❌ Placeholder content ("TBD", "Coming soon", "To be documented")
- ❌ Documenting implementation details users don't need
- ❌ Creating separate "Examples" section when examples should be inline
