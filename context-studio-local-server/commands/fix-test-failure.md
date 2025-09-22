# Fix Test Failure

## Failing test: $ARGUMENTS

Generate a complete plan for fixing test failure(s) with thorough research. Ensure context is passed to the AI agent to enable self-validation and iterative refinement. Read the test failure description (the provided file or github issue) first to understand what needs to be fixed.

The AI agent only gets the context you are appending to the fix plan and training data. Assume the AI agent has access to the codebase and the same knowledge cutoff as you, so its important that your research findings are included or referenced in the fix plan. The Agent has Websearch capabilities, so pass urls to documentation and examples.

Use the github cli to create an issue for the test failure:
- label the issue 'backend' or 'ux' as appropriate
- label the issue as 'test failure'

## Research Process

1. **Failure Analysis**
   - Execute the test and capture the log output
   - Document the failure in the github issue

2. **Codebase Analysis**
   - Determine what functionality the test is verifying
   - Determine the correct behavior
   - Determine what the test is attempting to accomplish
   - Document this analysis as a comment on the github issue

3. **Fix Test or Fix Functionality**
   - The most important decision you are making is whether to fix the code or fix the implementation
   - If the test is designed to test code that doesn't exist, determine if the missing functionality is critical to the feature being tested
      - If the missing functionality is critical, fix the functionality of the application
      - If the missing functionality is not critical, fix the test

## Fix Design

Design a fix for the issue based on your analysis.

Document the fix as a comment on the github issue before you begin implementing the fix.

## Fix Implementation

- Implement the fix according to your design
- Execute the test to confirm the fix
- Iterate until the fix is successful
- Fix all unexpected errors and warnings that are logged in the course of executing the test

## Fix Documentation

When the test is successful:
- Commit your changes and push them up to the remote, note the github issue in the commit message
- Add a comment to the github issue with the commit hash
- Close the github issue