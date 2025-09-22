# Execute BASE PRP

Implement a feature using using the PRP file or a PRP documented in a github issue.

## PRP File or github issue: $ARGUMENTS

## Execution Process

1. **Load PRP**
   - Read the specified PRP file or github issue which documents the PRP
      - If given a github issue, use the github cli or mcp server to load the issue
   - Understand all context and requirements
   - Follow all instructions in the PRP and extend the research if needed
   - Ensure you have all needed context to implement the PRP fully
   - Do more web searches and codebase exploration as needed

2. **THINK HARDER**
   - Think hard before you execute the plan. Create a comprehensive plan addressing all requirements.
   - Break down complex tasks into smaller, manageable steps using your todos tools.
   - If a github issue was not provided, use the TodoWrite tool to create and track your implementation plan.
   - Identify implementation patterns from existing code to follow.

3. **Execute the plan**
   - If provided with a github issue, load the body and all comments
   - Execute the PRP
   - Implement all the code

4. **Validate**
   - Run each validation command
   - Fix any failures
   - Re-run until all pass

5. **Complete**
   - Ensure all checklist items done
   - Run final validation suite
   - Run all unit and integration tests (all tests, not just those you know were affected) and fix all failures as well as unexpected warnings and errors
   - Report completion status
   - Read the PRP again to ensure you have implemented everything
   - If provided a github issue, leave it open pending review
   - If provided a github issue, commit your changes and push them up to the remote
   - If provided a github issue, check for an open pull request for that branch, if one doesn't exist please create one

6. **Reference the PRP**
   - You can always reference the PRP again if needed

Note: If validation fails, use error patterns in PRP to fix and retry.