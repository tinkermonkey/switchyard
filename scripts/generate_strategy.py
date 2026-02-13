#!/usr/bin/env python3
"""
Strategy Generator

Uses LLM to generate intelligent agent/skill strategy based on codebase analysis.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import Claude Code integration
from claude.claude_integration import run_claude_code

# Configure logging
logger = logging.getLogger(__name__)


def extract_json_from_response(text: str) -> str:
    """
    Extract JSON from markdown code blocks or raw text

    Args:
        text: Response text potentially containing JSON

    Returns:
        Extracted JSON string
    """
    # Try to find JSON in markdown code block
    json_block_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
    match = re.search(json_block_pattern, text, re.DOTALL)

    if match:
        return match.group(1)

    # If no code block, try to find JSON directly using proper JSON decoder
    start = text.find('{')
    if start == -1:
        return text

    # Use JSONDecoder for proper parsing (handles strings with braces correctly)
    try:
        decoder = json.JSONDecoder()
        obj, end_idx = decoder.raw_decode(text, start)
        return json.dumps(obj)
    except json.JSONDecodeError:
        # Fallback to simple extraction if decoder fails
        logger.warning("JSON decoder failed, using fallback extraction")
        return text[start:]


async def generate_strategy_with_llm(
    project: str,
    analysis: Dict[str, Any],
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Generate strategy using Claude Code CLI via run_claude_code()

    Args:
        project: Project name
        analysis: Codebase analysis results
        config: Project configuration

    Returns:
        Generated strategy with agents and skills
    """
    logger.info(f"Generating strategy for {project} using Claude Code CLI...")

    # Extract key info from analysis with safe defaults
    try:
        languages = analysis.get('tech_stacks', {}).get('languages', [])
        frameworks = analysis.get('tech_stacks', {}).get('frameworks', [])
        test_framework = analysis.get('testing', {}).get('test_framework', 'unknown')
        has_tests = analysis.get('testing', {}).get('test_count', 0) > 0
        has_docker = analysis.get('deployment', {}).get('docker', False)
        has_ci_cd = analysis.get('deployment', {}).get('ci_cd', 'none') != 'none'
        detected_layers = analysis.get('structure', {}).get('detected_layers', [])
    except Exception as e:
        logger.error(f"Failed to extract data from analysis dict: {e}")
        logger.error(f"Analysis dict keys: {list(analysis.keys())}")
        raise ValueError(f"Malformed analysis dict - missing expected structure: {e}")

    # Build prompt
    prompt = f"""You are an expert at designing AI agent teams for software projects.

Given this codebase analysis for the **{project}** project:

**Technology Stack:**
- Languages: {', '.join(languages)}
- Frameworks: {', '.join(frameworks) if frameworks else 'None detected'}
- Test framework: {test_framework}
- Has tests: {has_tests}
- Docker: {has_docker}
- CI/CD: {has_ci_cd}

**Architecture:**
- Detected layers: {', '.join(detected_layers) if detected_layers else 'None detected'}
- Total files: {analysis.get('structure', {}).get('total_files', 0)}
- Total directories: {analysis.get('structure', {}).get('total_dirs', 0)}

**Dependencies:**
- Critical dependencies: {', '.join(analysis.get('dependencies', {}).get('critical', [])[:10]) if analysis.get('dependencies', {}).get('critical') else 'None'}

Design an optimal team of project-specific agents and skills for this codebase.

**Required Agents** (always create these 3):
1. **{project}-architect**: Expert in this codebase's architecture, can explain how components work together
2. **{project}-guardian**: Enforces architectural standards and catches antipatterns
3. **{project}-doc-maintainer**: Maintains project documentation and README

**Conditional Agents** (create only if applicable):
- **{project}-tester**: If test framework detected and tests exist
- **{project}-deployer**: If deployment configuration exists (Docker/CI-CD)
- **{project}-api-expert**: If API framework detected (fastapi, express, etc.)
- **{project}-data-expert**: If database/ORM detected (sqlalchemy, mongoose, etc.)

**Skills** (create 3-7 skills that provide quick-reference utilities):
- **{project}-architecture**: Show architectural overview
- **{project}-test**: Run tests (if testing exists)
- **{project}-deploy**: Deployment procedures (if deployment exists)
- Framework-specific patterns and common commands

**Important Guidelines:**
1. Only create agents that add value for THIS specific project
2. Agents should have clear, non-overlapping responsibilities
3. Skills should be practical, user-invocable utilities
4. Match tool access to agent needs (not all agents need all tools)
5. Choose model based on complexity: "opus" for complex reasoning, "sonnet" for most tasks

**Output Format:**
Return ONLY a JSON object (no markdown, no explanations outside JSON) with this structure:

{{
  "agents": [
    {{
      "name": "{project}-<capability>",
      "purpose": "One-line description of what this agent does",
      "model": "sonnet" | "opus",
      "tools": ["Bash", "Read", "Grep", "Glob", "Edit", "Write"],
      "color": "blue" | "green" | "purple" | "orange",
      "rationale": "Why this agent is needed for this project"
    }}
  ],
  "skills": [
    {{
      "name": "{project}-<skill>",
      "purpose": "One-line description",
      "args": "<arg-spec>" | "",
      "rationale": "Why this skill is useful"
    }}
  ],
  "rationale": "Overall strategy explanation (2-3 sentences)"
}}

Now generate the strategy:"""

    # Determine orchestrator root (script is in scripts/ subdirectory)
    script_dir = Path(__file__).parent.parent  # Go up from scripts/ to orchestrator root
    orchestrator_root = Path(os.environ.get('ORCHESTRATOR_ROOT', str(script_dir)))

    # Build context for run_claude_code()
    context = {
        'project': project,
        'agent': 'strategy_generator',
        'task_id': f'strategy-{project}',
        'use_docker': False,  # Run locally, no Docker needed
        'work_dir': str(orchestrator_root),
        'claude_model': 'claude-sonnet-4-5-20250929',
        'observability': None,  # Optional: emit events if orchestrator running
    }

    try:
        # Call Claude Code CLI via run_claude_code()
        response_text = await run_claude_code(prompt, context)

        # Handle both dict and string responses
        if isinstance(response_text, dict):
            response_text = response_text.get('result', '')

        # Validate response is not empty
        if not response_text or not response_text.strip():
            raise ValueError(
                "Claude Code returned an empty response. This may indicate an API error, "
                "authentication issue, or network problem. Check CLAUDE_CODE_OAUTH_TOKEN "
                "or ANTHROPIC_API_KEY environment variable."
            )

        logger.debug(f"Raw response: {response_text[:500]}...")

        # Parse JSON response
        strategy_json = extract_json_from_response(response_text)
        strategy = json.loads(strategy_json)

        # Validate structure
        if 'agents' not in strategy or 'skills' not in strategy:
            raise ValueError("Strategy missing required fields 'agents' or 'skills'")

        logger.info(f"  ✓ Generated strategy: {len(strategy['agents'])} agents, {len(strategy['skills'])} skills")

        # Save strategy (reuse orchestrator_root from context building)
        output_dir = orchestrator_root / 'state' / 'projects' / project
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / 'generation_strategy.json'

        with open(output_file, 'w') as f:
            json.dump(strategy, f, indent=2)

        logger.info(f"  ✓ Strategy saved to: {output_file}")

        return strategy

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse strategy JSON: {e}")
        # Use locals() to safely access response_text
        if 'response_text' in locals():
            logger.error(f"Raw response: {response_text}")
        raise ValueError(f"Invalid JSON in strategy response: {e}")
    except Exception as e:
        logger.error(f"Strategy generation error: {e}")
        raise


def display_strategy(project: str, strategy: Dict[str, Any]):
    """
    Display strategy for user review

    Args:
        project: Project name
        strategy: Generated strategy
    """
    print(f"\n{'='*60}")
    print(f"Agent Team Strategy for {project}")
    print(f"{'='*60}\n")

    print(f"**Agents to Generate** ({len(strategy['agents'])})")
    for agent in strategy['agents']:
        print(f"  • {agent['name']}: {agent['purpose']}")
        print(f"    Model: {agent.get('model', 'sonnet')}, Tools: {', '.join(agent.get('tools', ['Bash', 'Read']))}")

    print(f"\n**Skills to Generate** ({len(strategy['skills'])})")
    for skill in strategy['skills']:
        args_display = f" {skill['args']}" if skill.get('args') else ""
        print(f"  • {skill['name']}{args_display}: {skill['purpose']}")

    print(f"\n**Rationale**:")
    print(f"{strategy.get('rationale', 'No rationale provided')}\n")


def confirm_strategy() -> bool:
    """
    Get user confirmation for strategy

    Returns:
        True if user confirms, False otherwise
    """
    while True:
        response = input("Proceed with this strategy? [Y/n]: ").strip().lower()

        if response in ['', 'y', 'yes']:
            return True
        elif response in ['n', 'no']:
            return False
        else:
            print("Please enter 'y' or 'n'")


async def main():
    """CLI entry point for testing"""
    import argparse

    parser = argparse.ArgumentParser(description="Generate agent team strategy")
    parser.add_argument('project', help='Project name')
    parser.add_argument('--analysis', help='Path to analysis JSON (default: auto-detect)')
    parser.add_argument('--auto-approve', action='store_true', help='Skip confirmation')
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # Load analysis
    if args.analysis:
        analysis_file = Path(args.analysis)
    else:
        analysis_file = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / 'state' / 'projects' / args.project / 'codebase_analysis.json'

    if not analysis_file.exists():
        logger.error(f"Analysis file not found: {analysis_file}")
        logger.error("Run analyze_codebase.py first")
        sys.exit(1)

    try:
        with open(analysis_file, 'r') as f:
            analysis = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load analysis: {e}")
        sys.exit(1)

    # Load project config (or use empty config)
    config = {}

    try:
        strategy = await generate_strategy_with_llm(args.project, analysis, config)

        display_strategy(args.project, strategy)

        if not args.auto_approve:
            if not confirm_strategy():
                print("  ⊗ Strategy rejected")
                sys.exit(1)

        print("  ✓ Strategy approved")

    except Exception as e:
        logger.error(f"✗ Strategy generation failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
