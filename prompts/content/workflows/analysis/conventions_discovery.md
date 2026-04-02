---
invoked_by: scripts/analyze_codebase.py — discover_conventions() via load_prompt("analysis/conventions_discovery", project=project)
variables:
  project: Project name
---

# Coding Conventions & Patterns Discovery

You are analyzing the **{project}** project to understand its coding conventions.

## Your Mission

Read code samples and documentation to extract coding standards, patterns, and best practices used in this specific project.

### Step 1: Read Project Guidelines

Find and thoroughly read:
- `CLAUDE.md` - Primary source of coding conventions
- `CONTRIBUTING.md` - Contribution guidelines
- `README.md` - May contain coding standards section
- `docs/` directory - Look for style guides, patterns docs

### Step 2: Sample Representative Files

Read 10-15 well-structured files from different parts of the codebase:
- Entry points (main.py, index.ts, etc.)
- Domain models (models.py, entities/, etc.)
- Business logic (services/, handlers/, etc.)
- Tests (to understand testing patterns)

### Step 3: Extract Patterns from Code

Identify recurring patterns:
- **Naming Conventions**: snake_case, camelCase, file naming patterns
- **Code Organization**: How are files structured? Imports organized?
- **Type Annotations**: Comprehensive, partial, or absent?
- **Error Handling**: Exceptions, Result types, error propagation
- **Async Patterns**: Consistent async/await usage, callback patterns
- **Immutability**: Frozen dataclasses, readonly, const usage
- **Documentation**: Docstring style, comment patterns
- **Configuration**: How is config managed and passed around?

### Step 4: Identify Antipatterns to Avoid

From CLAUDE.md or code analysis, note:
- What patterns are explicitly discouraged?
- What architectural boundaries must not be crossed?
- What common mistakes should be avoided?

### Step 5: Return Patterns Summary

Return the comprehensive summary as your response (do not write files - the orchestrator will save it).

```markdown
# Coding Patterns & Conventions: {project}

## Conventions from CLAUDE.md
[Extract key guidelines from CLAUDE.md if it exists]

## Naming Conventions
- Files: [Pattern and examples]
- Classes: [Pattern and examples]
- Functions: [Pattern and examples]
- Variables: [Pattern and examples]

## Code Organization
- Import order: [How imports are organized]
- File structure: [Typical file layout]
- Module boundaries: [How code is separated]

## Type Safety & Annotations
- Style: [Comprehensive type hints? TypeScript strict mode?]
- Examples: [Show typical type usage]

## Error Handling
- Pattern: [Exceptions, Result types, etc.]
- Examples: [From actual code]

## Testing Conventions
- File naming: [test_*.py, *.test.ts, etc.]
- Test structure: [AAA pattern, fixtures, etc.]
- Async testing: [How async tests are handled]

## Common Patterns
[List 5-10 patterns found across multiple files]

## Antipatterns to Avoid
[List what NOT to do, from CLAUDE.md or code review]

## Best Practices Specific to This Project
[Unique conventions not found in other codebases]
```

**Important:**
- Prioritize CLAUDE.md - it's the authoritative source
- Be specific with examples (file:line references)
- Note both what TO do and what NOT to do
