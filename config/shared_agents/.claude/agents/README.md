# Shared Claude Code Agents

This directory contains specialist agents available to all orchestrator agents.

Claude Code automatically discovers agents in this directory when mounted into
agent containers.

## Available Agents

- **playwright_expert** - Playwright browser automation and testing
- **database_expert** - Database schema design and optimization
- **flowbite_react_expert** - Flowbite-React UI components
- **redis_expert** - Redis data structures and patterns (coming soon)

## Usage

Specialist agents are automatically available to Claude when running in agent containers.
Invoke them using `@agent-name` syntax:

```
@playwright_expert create tests for the login flow with email and password fields
```

Or let Claude invoke them automatically based on task context.

## Adding New Specialists

To add a new specialist agent:

1. Create a new markdown file in this directory (e.g., `api_design_expert.md`)
2. Follow the standard agent format with clear expertise and guidelines
3. Restart the orchestrator to make it available

No code changes needed - just add the markdown file!
