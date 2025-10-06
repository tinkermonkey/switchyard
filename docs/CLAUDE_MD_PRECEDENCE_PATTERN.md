# CLAUDE.md Precedence Pattern

## Problem Statement

When orchestrator agents execute in managed projects, there can be conflicts between:
1. Agent-specific prompts (from orchestrator)
2. Issue/discussion requirements (from GitHub)
3. Project-specific conventions (from project's CLAUDE.md)

Without clear precedence, agents may violate project conventions (e.g., creating markdown documentation files when the project's CLAUDE.md specifies "use GitHub issues for documentation").

## Solution: Explicit Deference to CLAUDE.md

### Design Principle

**Trust Claude Code CLI's CLAUDE.md discovery mechanism** - do not duplicate this functionality.

Claude Code CLI automatically discovers and loads CLAUDE.md files in this hierarchy:
- `~/.claude/CLAUDE.md` - Global user preferences
- `/workspace/CLAUDE.md` - Project root conventions
- `/workspace/subdir/CLAUDE.md` - Directory-specific conventions

This context is maintained throughout the entire session and influences every tool call.

### Implementation

All agent prompts now include an explicit instruction to defer to CLAUDE.md:

```
**PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first.
The project's CLAUDE.md file defines project-specific conventions, file organization,
and documentation requirements that take precedence over these general instructions.
```

This is injected in two locations:
1. **base_maker_agent.py:146,159** - Base class output instructions
2. **senior_software_engineer_agent.py:136** - Implementation-specific prompts

### Precedence Order

When conflicts arise, the agent should follow this precedence:
1. **HIGHEST**: Project's CLAUDE.md conventions
2. **HIGH**: Specific task requirements (issue description, acceptance criteria)
3. **MEDIUM**: Agent role-specific best practices
4. **LOW**: General orchestrator instructions

### Example: Documentation Location

**Scenario**: Issue #102 requested "Documentation updated" as an acceptance criterion.

**Without explicit precedence**:
- Agent creates PHASE4_IMPLEMENTATION_SUMMARY.md, PHASE4_GITHUB_SUMMARY.md, etc.
- Violates project's CLAUDE.md: "Use GitHub issues for change documentation"

**With explicit precedence**:
- Agent reads CLAUDE.md first
- Recognizes project uses GitHub issues for documentation
- Interprets "Documentation updated" as "update GitHub issue tasks"
- Posts summary to GitHub instead of creating markdown files

### Validation

To verify CLAUDE.md is being followed, monitor for:

1. **File creation patterns** - Check if files are created in locations forbidden by CLAUDE.md
2. **Documentation conventions** - Verify documentation is created in the specified format (GitHub issues vs. markdown files)
3. **Directory structure** - Ensure files are placed according to CLAUDE.md's repo structure

### Future Enhancements (Optional)

If explicit deference proves insufficient, consider:

1. **Post-execution validation** - services/output_validator.py to check compliance
2. **Preventive controls** - Filesystem constraints in project config
3. **CLAUDE.md loading verification** - Log when CLAUDE.md is discovered by Claude Code CLI

However, the current approach (explicit deference) should be sufficient given Claude Code CLI's robust CLAUDE.md discovery.

## References

- agents/base_maker_agent.py:117-167 - Output instruction builder
- agents/senior_software_engineer_agent.py:134-144 - Implementation prompt
- context-studio/CLAUDE.md:17-23 - Example project conventions
