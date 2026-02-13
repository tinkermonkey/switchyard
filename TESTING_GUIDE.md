# Agent Generation Testing Guide

Quick guide to test the revamped prompt-driven agent generation system.

## Prerequisites

1. **Authentication**: Ensure Claude Code CLI is authenticated
   ```bash
   # Check if authenticated
   claude --version

   # Set authentication (choose one):
   export CLAUDE_CODE_OAUTH_TOKEN="..."  # Subscription billing
   # OR
   export ANTHROPIC_API_KEY="..."        # API billing
   ```

2. **Project Setup**: Ensure you have a project to test with
   ```bash
   # List available projects
   ls /workspace/

   # Recommended test projects:
   # - rounds (Python, pytest, async patterns)
   # - context-studio (if available)
   ```

## Test 1: Dry Run (Safe)

Test the entire workflow without writing files:

```bash
# From orchestrator root (clauditoreum/)
python scripts/maintain_agent_team.py --project rounds --dry-run
```

**Expected Output:**
```
Discovering projects for agent generation...
Found 1 project(s): rounds

============================================================
Agent Team Generation: rounds
============================================================

Phase 1: Detecting codebase changes...
  Change level: high
  Reason: Initial generation

[DRY RUN] Would proceed with generation workflow:
  Phase 2: Analyze codebase
  Phase 3: Generate strategy
  Phase 4: User review (unless --auto-approve)
  Phase 5: Generate artifacts
  Phase 6: Validate artifacts
  Phase 7: Deploy to .claude/
  Phase 8: Update state
```

## Test 2: Codebase Analysis Only

Test the new prompt-driven analysis:

```bash
# Run just the analysis phase
python scripts/analyze_codebase.py rounds
```

**Expected Output:**
```
Running codebase analysis for rounds...
Phase 1: Discovering architecture...
  Running architecture discovery with Claude Code CLI...
  ✓ Created: ArchitectureSummary.md
Phase 2: Discovering tech stack...
  Running tech stack discovery with Claude Code CLI...
  ✓ Created: TechStackSummary.md
Phase 3: Discovering patterns & conventions...
  Running conventions discovery with Claude Code CLI...
  ✓ Created: PatternsSummary.md
  ✓ Analysis saved to: state/projects/rounds/codebase_analysis.json

✓ Analysis complete for rounds
  Summaries created:
    - ArchitectureSummary.md
    - TechStackSummary.md
    - PatternsSummary.md
```

**Verify Summaries Created:**
```bash
# Check summary files exist
ls -lh /workspace/rounds/.claude/clauditoreum/

# Expected files:
# - ArchitectureSummary.md
# - TechStackSummary.md
# - PatternsSummary.md

# Preview architecture summary
head -n 50 /workspace/rounds/.claude/clauditoreum/ArchitectureSummary.md
```

**Quality Checks:**
- ✅ Summaries reference actual files from the project
- ✅ Patterns cite specific file:line locations
- ✅ Architecture summary describes actual structure (not generic)
- ✅ Tech stack includes research notes for unfamiliar technologies

## Test 3: Strategy Generation

Test strategy generation using the summaries:

```bash
# Run strategy generation (requires analysis from Test 2)
python scripts/generate_strategy.py rounds
```

**Expected Output:**
```
Generating strategy for rounds using Claude Code CLI...
  ✓ Generated strategy: 4 agents, 4 skills
  ✓ Strategy saved to: state/projects/rounds/generation_strategy.json

============================================================
Agent Team Strategy for rounds
============================================================

**Agents to Generate** (4)
  • rounds-architect: Expert in hexagonal architecture...
  • rounds-guardian: Enforces frozen dataclass patterns...
  • rounds-tester: Runs pytest-asyncio tests...
  • rounds-doc-maintainer: Maintains CLAUDE.md and docs...

**Skills to Generate** (4)
  • rounds-architecture: Show architectural overview
  • rounds-test: Run pytest-asyncio tests
  • rounds-deploy: Docker deployment procedures
  • rounds-patterns: Show coding patterns

**Rationale:**
This project uses hexagonal architecture with pytest-asyncio...

Proceed with this strategy? [Y/n]:
```

**Quality Checks:**
- ✅ Agents reference actual architectural patterns from summaries
- ✅ Rationale mentions specific technologies found
- ✅ No generic agents - all tailored to project specifics
- ✅ Skills provide practical project-specific utilities

## Test 4: Full Generation (Auto-Approve)

Run the complete workflow with auto-approval:

```bash
# Full generation without user prompts
python scripts/maintain_agent_team.py --project rounds --auto-approve
```

**Expected Output:**
```
============================================================
Agent Team Generation: rounds
============================================================

Phase 1: Detecting codebase changes...
  Change level: high
  Reason: Initial generation

Phase 2: Analyzing codebase...
Phase 1: Discovering architecture...
  ✓ Created: ArchitectureSummary.md
Phase 2: Discovering tech stack...
  ✓ Created: TechStackSummary.md
Phase 3: Discovering patterns & conventions...
  ✓ Created: PatternsSummary.md
  ✓ Analysis complete (summaries created)

Phase 3: Generating strategy...
  ✓ Strategy generated (4 agents, 4 skills)

Phase 4: Strategy auto-approved (--auto-approve flag)

Phase 5: Generating artifacts...
  Generating 4 agent(s)...
  ✓ Generated agent: rounds-architect.md
  ✓ Generated agent: rounds-guardian.md
  ✓ Generated agent: rounds-tester.md
  ✓ Generated agent: rounds-doc-maintainer.md
  Generating 4 skill(s)...
  ✓ Generated skill: rounds-architecture
  ✓ Generated skill: rounds-test
  ✓ Generated skill: rounds-deploy
  ✓ Generated skill: rounds-patterns
  ✓ Generated 4 agents, 4 skills

Phase 6: Validating artifacts...
  ✓ All 8 artifact(s) validated

Phase 7: Artifacts deployed to rounds/.claude/

Phase 8: Updating state...
  ✓ State updated

============================================================
Generation Complete
============================================================
  Agents: 4
  Skills: 4
  Validation: 8/8 passed
```

**Verify Artifacts:**
```bash
# Check agents created
ls -lh /workspace/rounds/.claude/agents/rounds-*.md

# Check skills created
ls -lh /workspace/rounds/.claude/skills/rounds-*/

# Check state updated
cat state/projects/rounds/agent_generation_state.yaml
```

## Test 5: Cleanup

Test artifact cleanup with per-project state:

```bash
# Preview what would be cleaned up
python scripts/cleanup_artifacts.py rounds --dry-run

# Actually cleanup (if needed)
python scripts/cleanup_artifacts.py rounds
```

**Expected Behavior:**
- ✅ Identifies orphaned artifacts (not in project state)
- ✅ Identifies artifacts with validation failures
- ✅ Refuses to cleanup if no state file exists (safety check)
- ✅ Archives artifacts before deletion

## Common Issues

### Issue: "Claude CLI not found"
**Solution:**
```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Or check PATH
which claude
```

### Issue: "Analysis failed: API error"
**Solution:**
```bash
# Check authentication
echo $CLAUDE_CODE_OAUTH_TOKEN
echo $ANTHROPIC_API_KEY

# Re-authenticate if needed
claude auth login
```

### Issue: "Summaries not created"
**Solution:**
```bash
# Check Claude Code CLI can write files
cd /workspace/rounds
claude "Create a test file: test.txt with content 'hello'"
ls test.txt
rm test.txt
```

### Issue: "No changes detected" (subsequent runs)
**Explanation:** Codebase hash unchanged - skip generation to save costs

**Force Regeneration:**
```bash
# Delete analysis hash to force regeneration
rm state/projects/rounds/agent_generation_state.yaml

# Run again
python scripts/maintain_agent_team.py --project rounds --auto-approve
```

## Quality Validation

After generation, manually review summaries:

### Architecture Summary
```bash
# Should include:
# - Specific architectural style (hexagonal, layered, etc.)
# - Actual directory structure
# - Cited files with evidence
# - NOT generic descriptions

cat /workspace/rounds/.claude/clauditoreum/ArchitectureSummary.md | grep -A 5 "Architectural Style"
```

### Tech Stack Summary
```bash
# Should include:
# - Actual dependencies from requirements.txt/package.json
# - Research notes for unfamiliar libraries
# - Code patterns detected from sampling
# - NOT just a list of frameworks

cat /workspace/rounds/.claude/clauditoreum/TechStackSummary.md | grep -A 10 "Research Notes"
```

### Patterns Summary
```bash
# Should include:
# - CLAUDE.md conventions (if exists)
# - Actual code patterns with file:line citations
# - Antipatterns to avoid
# - NOT generic best practices

cat /workspace/rounds/.claude/clauditoreum/PatternsSummary.md | grep -A 5 "Antipatterns"
```

## Performance Benchmarks

Expected timing for a medium-sized project (~100 files):

- Phase 1 (Change Detection): < 1 second
- Phase 2 (Analysis - 3 Claude calls): 2-5 minutes
- Phase 3 (Strategy - 1 Claude call): 30-60 seconds
- Phase 4 (User Review): Manual
- Phase 5 (Artifact Generation): < 5 seconds
- Phase 6 (Validation): < 2 seconds
- Phase 7 (Deployment): < 1 second
- Phase 8 (State Update): < 1 second

**Total:** ~3-7 minutes (mostly Claude API time)

## Success Criteria

Generation is successful if:

1. ✅ All 3 summary files created
2. ✅ Summaries reference actual project files
3. ✅ Strategy includes project-specific agents
4. ✅ All artifacts validate successfully
5. ✅ State file created with artifact tracking
6. ✅ No syntax errors in generated markdown

## Next Steps After Testing

1. Review generated agents for quality
2. Test invoking a generated agent: `claude --agent rounds-architect "explain the architecture"`
3. Refine prompts if summaries are too generic
4. Update AGENT_TEAM_MAINTAINER.md documentation with examples
