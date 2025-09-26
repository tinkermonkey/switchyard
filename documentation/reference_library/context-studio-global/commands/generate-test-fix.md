# Create Fix Plan

## Failure file or issue: $ARGUMENTS

- If provided a github url, use the github cli to load the issue

Generate a complete plan for fixing test failure(s) with thorough research. Ensure context is passed to the AI agent to enable self-validation and iterative refinement. Read the test failure description (the provided file or github issue) first to understand what needs to be fixed.

The AI agent only gets the context you are appending to the fix plan and training data. Assume the AI agent has access to the codebase and the same knowledge cutoff as you, so its important that your research findings are included or referenced in the fix plan. The Agent has Websearch capabilities, so pass urls to documentation and examples.

If provided a github issue as your input, document your fix plan as a comment on the issue using the github command line tool.

## Research Process

1. **Codebase Analysis**
   - Search for similar patterns in the codebase
   - Identify files to reference in fix plan
   - Note existing conventions to follow
   - Check test patterns for validation approach
   - Be mindful of the file paths, you have the full view of the repo but the tests are run in the sub-directories of the application:
      - Back-end fixes are executed inside of /local-server using python tools
      - Ux fixes are executed inside of /ux using the javascript / typescript tools

2. **Fix Test or Fix Functionality**
   - The most important decision you are making is whether to fix the code or fix the implementation
   - If the test is designed to test code that doesn't exist, determine if the missing functionality is critical to the feature being tested
      - If the missing functionality is critical, fix the functionality of the application
      - If the missing functionality is not critical, fix the test

2. **External Research**
   - Search for similar failure modes/patterns online
   - Library documentation (include specific URLs)
   - Implementation examples (GitHub/StackOverflow/blogs)
   - Best practices and common pitfalls

3. **User Clarification** (if needed)
   - Specific patterns to mirror and where to find them?
   - Integration requirements and where to find them?

## Fix Plan Generation

### Critical changes identified as the Fix Plan
- **Documentation**: URLs with specific sections
- **Code Changes**: Detail the plan to implement the fix
- **Gotchas**: Library quirks, version issues
- **Patterns**: Examples in the codebase of patterns to follow

### Validation Gates (Must be Executable) eg for python or javascript / typescript
```bash
# Syntax/Style
ruff check --fix && mypy .

# Unit Tests
uv run pytest tests/ -v

```

*** CRITICAL AFTER YOU ARE DONE RESEARCHING AND EXPLORING THE CODEBASE BEFORE YOU START WRITING THE FIX PLAN ***

## Output
- If not provided with a github issue, save as: `PRPs/{feature-name}.md`
- If provided with a github issue, document the fix plan in a comment on the issue
- If provided with a github issue, label it as 'test fix'

## Quality Checklist
- [ ] All necessary context included
- [ ] Validation gates are executable by AI
- [ ] References existing patterns
- [ ] Clear implementation path
- [ ] Error handling documented

Leave the issue open until the fix is confirmed.

Remember: The goal is one-pass implementation success through comprehensive context.