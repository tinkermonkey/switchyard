# ux/CLAUDE.md

## Git Workflow Rules

You are operating in an automated git workflow. Follow these rules:

### Current Branch Context
- You can determine your current branch with `git branch --show-current`
- Feature branches are named: `feature/#<issue>-<description>`
- NEVER switch branches yourself - the orchestrator manages this

### Commit Practices
- Make atomic commits for each logical change
- Use conventional commit format: `type(scope): description`
  - feat: new feature
  - fix: bug fix
  - refactor: code refactoring
  - test: adding tests
  - docs: documentation
  
### Working Directory
- Always check `git status` before making changes
- Stage files incrementally with `git add <specific-files>`
- Commit frequently but meaningfully

### Example Workflow Check
```bash
# Start of any task
git status
git branch --show-current
# Proceed with work only if on correct feature branch