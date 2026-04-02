---
invoked_by: scripts/analyze_codebase.py — discover_tech_stack() via load_prompt("analysis/techstack_discovery", project=project)
variables:
  project: Project name
---

# Tech Stack Discovery & Research

You are analyzing the **{project}** project to understand and document its technology stack.

## Your Mission

Discover all technologies used, research unfamiliar ones, and create a comprehensive tech stack summary.

### Step 1: Find Dependency Files

Search for and read:
- Python: `pyproject.toml`, `requirements.txt`, `Pipfile`, `setup.py`
- JavaScript/TypeScript: `package.json`, `yarn.lock`, `pnpm-lock.yaml`
- Go: `go.mod`
- Rust: `Cargo.toml`
- Java: `pom.xml`, `build.gradle`
- Ruby: `Gemfile`
- PHP: `composer.json`
- .NET: `*.csproj`, `*.fsproj`

Look at ALL depths (root, nested directories) - don't assume files are at the root.

### Step 2: Extract Dependencies

For each dependency file found:
1. Parse and list all dependencies (including dev/optional dependencies)
2. Categorize by purpose:
   - **Web Frameworks**: FastAPI, Express, Django, etc.
   - **Testing**: pytest, Jest, etc.
   - **Data/ORM**: SQLAlchemy, TypeORM, etc.
   - **Async/Concurrency**: asyncio, tokio, etc.
   - **Type Safety**: Pydantic, Zod, etc.
   - **Build Tools**: webpack, vite, etc.

### Step 3: Research Unfamiliar Technologies

For each significant dependency you don't recognize:
1. Use WebSearch to find documentation
2. Understand:
   - What does it do?
   - What category of tool is it?
   - What are common patterns/best practices?
   - How does it typically structure code?

### Step 4: Detect Testing Approach

Analyze test files and configurations:
- What test framework is used?
- Where are tests located?
- Are there async test patterns?
- Test coverage approach?

### Step 5: Sample Code for Patterns

Read 5-10 key source files to detect:
- **Language features**: Type hints, async/await, pattern matching
- **Coding style**: Immutability, functional vs OOP, etc.
- **Dependency injection**: Constructor injection, frameworks
- **Error handling**: Exceptions, Result types, etc.

### Step 6: Return Tech Stack Summary

Return the comprehensive summary as your response (do not write files - the orchestrator will save it).

Include:

```markdown
# Tech Stack Summary: {project}

## Language & Runtime
- Primary Language: [Python 3.11, TypeScript, etc.]
- Runtime: [Node.js, Python interpreter, etc.]

## Major Frameworks & Libraries

### Web Framework
- **Name**: [FastAPI, Express, etc.]
- **Purpose**: [What it does]
- **Best Practices**: [Key patterns from research]

### Testing Framework
- **Name**: [pytest-asyncio, Jest, etc.]
- **Location**: [Where tests live]
- **Patterns**: [How tests are structured]

[Repeat for each major category]

## Development Tools
- Build: [webpack, poetry, cargo, etc.]
- Linting: [ruff, eslint, etc.]
- Type Checking: [mypy, tsc, etc.]

## Deployment & Infrastructure
- Containerization: [Docker, none]
- CI/CD: [GitHub Actions, Jenkins, etc.]

## Code Patterns Detected
- **Async/Await**: [Evidence from code samples]
- **Type Safety**: [Type hints, interfaces, etc.]
- **Immutability**: [Frozen dataclasses, const, etc.]
- **Dependency Injection**: [Constructor injection, frameworks]

## Dependencies List
[Complete list of dependencies with brief descriptions]
```

**Research Notes:**
- For each technology you researched, include a brief note on what you learned
- If you found particularly useful documentation, link it

**Important:**
- Use WebSearch liberally for technologies you don't recognize
- Be comprehensive - find ALL dependency files (they may be nested)
- Cite specific files and line numbers for patterns you detect
