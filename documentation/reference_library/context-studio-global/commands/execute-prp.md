# Execute BASE PRP

Implement a feature using using the PRP file or a PRP documented in a github issue.

## PRP File or github issue: $ARGUMENTS

## Execution Process

1. **Load PRP**
   - Read the specified PRP file or github issue which documents the PRP
      - If given a github url, use the github cli to load the issue
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
   - If provided with a github issue, create a new feature or bug branch for your changes and check it out before beginning development
      - Stash and uncomitted changes before checking out the new branch
   - Execute the PRP
   - Implement all the code

4. **Validate**
   - Run each validation command
   - Fix any failures
   - Re-run until all pass

5. **Complete**
   - Ensure all checklist items done
   - Run final validation suite
   - Report completion status
   - Read the PRP again to ensure you have implemented everything
   - If provided with a github issue, leave it open pending review
   - If provided with a github issue, create a github pull request to merge your feature or bug branch into main and update the PR with your notes

6. **Reference the PRP**
   - You can always reference the PRP again if needed

Note: If validation fails, use error patterns in PRP to fix and retry.