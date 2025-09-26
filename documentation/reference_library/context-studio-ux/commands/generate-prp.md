# Create PRP

## Feature file or issue: $ARGUMENTS

- If provided a github url, use the github cli to load the issue

Generate a complete PRP for general feature implementation with thorough research. Ensure context is passed to the AI agent to enable self-validation and iterative refinement. Read the feature file first to understand what needs to be created, how the examples provided help, and any other considerations.

The AI agent only gets the context you are appending to the PRP and training data. Assume the AI agent has access to the codebase and the same knowledge cutoff as you, so its important that your research findings are included or referenced in the PRP. The Agent has Websearch capabilities, so pass urls to documentation and examples.

If provided a github issue as your input, document your PRP as one or more github sub-issues using the github command line tool. Create one sub-issue for the server and one sub-issue for the front end for cross-functional work and mark the front-end issue blocked by the back-end issue.

## Research Process

1. **Codebase Analysis**
   - Search for similar features/patterns in the codebase
   - Identify files to reference in PRP
   - Note existing conventions to follow
   - Check test patterns for validation approach
   - Don't design in backwards compatibility unless expressly asked, don't be afraid to break things
   - Avoid superlatives in your code naming ('advanced', 'optimized', 'enhanced', 'generic', etc), just name things functionally

2. **External Research**
   - Search for similar features/patterns online
   - Library documentation (include specific URLs)
   - Implementation examples (GitHub/StackOverflow/blogs)
   - Best practices and common pitfalls

3. **User Clarification** (if needed)
   - Specific patterns to mirror and where to find them?
   - Integration requirements and where to find them?

## PRP Generation

Using documentation/templates/prp_base.md as template:

### Critical Context to Include and pass to the AI agent as part of the PRP
- **Documentation**: URLs with specific sections
- **Code Examples**: Real snippets from codebase
- **Gotchas**: Library quirks, version issues
- **Patterns**: Existing approaches to follow

### Implementation Blueprint
- Start with pseudocode showing approach
- Reference real files for patterns
- Include error handling strategy
- list tasks to be completed to fullfill the PRP in the order they should be completed

### Validation Gates (Must be Executable) eg for python or javascript / typescript
```bash
# Syntax/Style
ruff check --fix && mypy .

# Unit Tests
uv run pytest tests/ -v

```

*** CRITICAL AFTER YOU ARE DONE RESEARCHING AND EXPLORING THE CODEBASE BEFORE YOU START WRITING THE PRP ***

*** THINK HARDER ABOUT THE PRP AND PLAN YOUR APPROACH THEN START WRITING THE PRP ***

## Output
If not provided with a github issue, save as: `PRPs/{feature-name}.md`
If provided with a github issue, create one or more sub-issues so that there is 1 issue for all back-end changes (if any) and 1 issue for all front-end changes (if any). Replicate the labels from the parent issue to the sub-issue(s) you create.

## Quality Checklist
- [ ] All necessary context included
- [ ] Validation gates are executable by AI
- [ ] References existing patterns
- [ ] Clear implementation path
- [ ] Error handling documented

Score the PRP on a scale of 1-10 (confidence level to succeed in one-pass implementation using claude codes)

If one or more github issues were created, label them with the appropriate type such as 'bug', 'enhancement', 'documentation'

Remember: The goal is one-pass implementation success through comprehensive context.