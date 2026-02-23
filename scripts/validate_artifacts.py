#!/usr/bin/env python3
"""
Artifact Validator

Multi-stage validation for generated agents and skills.
"""

import logging
import os
import re
import sys
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configure logging
logger = logging.getLogger(__name__)

# Constants
ORCHESTRATOR_ROOT = Path(os.environ.get('ORCHESTRATOR_ROOT', '.'))

# Valid tool names
VALID_TOOLS = [
    'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep',
    'WebSearch', 'WebFetch', 'Skill', 'Task',
    'NotebookEdit', 'AskUserQuestion', 'EnterPlanMode', 'ExitPlanMode'
]


def get_workspace_root() -> Path:
    """
    Get the workspace root directory

    Returns:
        Path to workspace root (/workspace in container, parent of orchestrator root outside)
    """
    if Path('/workspace').exists() and Path('/workspace').is_dir():
        return Path('/workspace')
    else:
        return ORCHESTRATOR_ROOT.parent


def get_project_claude_dir(project: str) -> Path:
    """
    Get .claude directory for a specific project

    Args:
        project: Project name

    Returns:
        Path to project's .claude directory
    """
    workspace_root = get_workspace_root()
    project_dir = workspace_root / project
    return project_dir / '.claude'


def validate_yaml_frontmatter(file_path: Path) -> Dict[str, Any]:
    """
    Stage 1: Validate YAML syntax and required fields

    Args:
        file_path: Path to artifact file

    Returns:
        Validation result with 'passed', 'errors', and optionally 'frontmatter'
    """
    try:
        with open(file_path, 'r') as f:
            content = f.read()

        # Check for frontmatter
        if not content.startswith('---\n'):
            return {'passed': False, 'errors': ['Missing YAML frontmatter']}

        # Split frontmatter
        parts = content.split('---\n', 2)
        if len(parts) < 3:
            return {'passed': False, 'errors': ['Incomplete YAML frontmatter (missing closing ---)']}

        # Parse frontmatter
        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            return {'passed': False, 'errors': [f'Invalid YAML syntax: {e}']}

        if not isinstance(frontmatter, dict):
            return {'passed': False, 'errors': ['Frontmatter must be a YAML dictionary']}

        # Check required fields
        required = ['name', 'description', 'generated']
        missing = [f for f in required if f not in frontmatter]
        if missing:
            return {'passed': False, 'errors': [f'Missing required field: {f}' for f in missing]}

        # Verify generated flag
        if not frontmatter.get('generated', False):
            return {'passed': False, 'errors': ['Missing or false "generated" flag']}

        return {'passed': True, 'errors': [], 'frontmatter': frontmatter}

    except FileNotFoundError:
        return {'passed': False, 'errors': ['File not found']}
    except Exception as e:
        return {'passed': False, 'errors': [f'File read error: {e}']}


def validate_markdown_syntax(file_path: Path) -> Dict[str, Any]:
    """
    Stage 2: Validate markdown structure

    Args:
        file_path: Path to artifact file

    Returns:
        Validation result with 'passed' and 'warnings'
    """
    warnings = []

    try:
        with open(file_path, 'r') as f:
            content = f.read()

        # Check for balanced code blocks
        code_block_count = content.count('```')
        if code_block_count % 2 != 0:
            warnings.append('Unbalanced code blocks (``` count is odd)')

        # Check for headers
        if not re.search(r'^#+ ', content, re.MULTILINE):
            warnings.append('No markdown headers found')

        # Check for very long lines (>500 chars)
        lines = content.split('\n')
        long_lines = [i+1 for i, line in enumerate(lines) if len(line) > 500]
        if long_lines:
            warnings.append(f'Very long lines found: {long_lines[:5]}')

        return {'passed': True, 'warnings': warnings}

    except Exception as e:
        return {'passed': False, 'warnings': [f'Markdown validation error: {e}']}


def validate_content_quality(file_path: Path) -> Dict[str, Any]:
    """
    Stage 3: Validate content completeness

    Args:
        file_path: Path to artifact file

    Returns:
        Validation result with 'passed' and 'errors'
    """
    try:
        with open(file_path, 'r') as f:
            content = f.read()

        # Check length
        if len(content) < 100:
            return {'passed': False, 'errors': ['Content too short (<100 chars)']}

        if len(content) > 50000:
            return {'passed': False, 'errors': ['Content too long (>50k chars)']}

        # Check for unfilled placeholders, excluding code blocks and inline code
        # to avoid false positives from legitimate code syntax (JS template literals,
        # JSX attribute bindings, bash variables, OpenAPI path params, etc.)
        non_code_content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        non_code_content = re.sub(r'`[^`\n]+`', '', non_code_content)
        placeholders = re.findall(r'\{[a-zA-Z_]+\}', non_code_content)
        if placeholders:
            return {'passed': False, 'errors': [f'Unfilled placeholders: {placeholders[:5]}']}

        # Check for TODO markers
        if re.search(r'\bTODO\b', content, re.IGNORECASE):
            return {'passed': False, 'errors': ['Contains TODO markers']}

        return {'passed': True, 'errors': []}

    except Exception as e:
        return {'passed': False, 'errors': [f'Content validation error: {e}']}


def validate_tool_references(file_path: Path, frontmatter: Dict) -> Dict[str, Any]:
    """
    Stage 4: Validate tool names

    Args:
        file_path: Path to artifact file
        frontmatter: Parsed frontmatter

    Returns:
        Validation result with 'passed' and 'warnings'
    """
    warnings = []

    tools = frontmatter.get('tools', '')
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(',') if t.strip()]
    elif isinstance(tools, list):
        tools = [str(t).strip() for t in tools]
    else:
        tools = []

    invalid = [t for t in tools if t not in VALID_TOOLS]
    if invalid:
        warnings.append(f'Unknown tools: {invalid}')

    return {'passed': True, 'warnings': warnings}


def validate_artifact(file_path: Path) -> Dict[str, Any]:
    """
    Run all validation stages on an artifact

    Args:
        file_path: Path to artifact file

    Returns:
        Complete validation result
    """
    results = {
        'file': str(file_path),
        'passed': True,
        'errors': [],
        'warnings': []
    }

    # Stage 1: YAML frontmatter
    yaml_result = validate_yaml_frontmatter(file_path)
    if not yaml_result['passed']:
        results['passed'] = False
        results['errors'].extend(yaml_result['errors'])
        return results  # Can't continue without valid frontmatter

    frontmatter = yaml_result['frontmatter']

    # Stage 2: Markdown syntax
    md_result = validate_markdown_syntax(file_path)
    results['warnings'].extend(md_result.get('warnings', []))

    # Stage 3: Content quality
    content_result = validate_content_quality(file_path)
    if not content_result['passed']:
        results['passed'] = False
        results['errors'].extend(content_result['errors'])

    # Stage 4: Tool references
    tool_result = validate_tool_references(file_path, frontmatter)
    results['warnings'].extend(tool_result.get('warnings', []))

    return results


def find_generated_artifacts(project: Optional[str] = None) -> Dict[str, List[Path]]:
    """
    Find all generated artifacts (optionally filtered by project)

    Args:
        project: Optional project name to filter by

    Returns:
        Dict with 'agents' and 'skills' lists of paths
    """
    artifacts = {
        'agents': [],
        'skills': []
    }

    if project is None:
        logger.warning("find_generated_artifacts() called without project parameter - this may find no artifacts")
        return artifacts

    # Get project-specific directories
    claude_dir = get_project_claude_dir(project)
    agents_dir = claude_dir / 'agents'
    skills_dir = claude_dir / 'skills'

    # Find agent files
    if agents_dir.exists():
        for agent_file in agents_dir.glob(f'{project}-*.md'):
            # Check if generated
            try:
                with open(agent_file, 'r') as f:
                    content = f.read()
                    if 'generated: true' in content:
                        artifacts['agents'].append(agent_file)
            except Exception:
                pass

    # Find skill files
    if skills_dir.exists():
        for skill_dir in skills_dir.glob(f'{project}-*'):
            if skill_dir.is_dir():
                skill_file = skill_dir / 'SKILL.md'
                if skill_file.exists():
                    # Check if generated
                    try:
                        with open(skill_file, 'r') as f:
                            content = f.read()
                            if 'generated: true' in content:
                                artifacts['skills'].append(skill_file)
                    except Exception:
                        pass

    return artifacts


def validate_all_generated_artifacts(project: Optional[str] = None) -> Dict[str, Any]:
    """
    Validate all generated artifacts (optionally filtered by project)

    Args:
        project: Optional project name to filter by

    Returns:
        Validation summary
    """
    # Find artifacts
    artifacts_dict = find_generated_artifacts(project)
    all_artifacts = artifacts_dict['agents'] + artifacts_dict['skills']

    results = {
        'total': len(all_artifacts),
        'passed': 0,
        'failed': 0,
        'files': []
    }

    # Validate each artifact
    for artifact in all_artifacts:
        result = validate_artifact(artifact)
        results['files'].append(result)

        if result['passed']:
            results['passed'] += 1
        else:
            results['failed'] += 1

    return results


def main():
    """CLI entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Validate generated artifacts")
    parser.add_argument('--project', help='Filter by project name')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed results')
    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(message)s')

    try:
        results = validate_all_generated_artifacts(args.project)

        print(f"\nValidation Results:")
        print(f"  Total: {results['total']}")
        print(f"  Passed: {results['passed']}")
        print(f"  Failed: {results['failed']}")

        if results['failed'] > 0:
            print(f"\nFailed artifacts:")
            for result in results['files']:
                if not result['passed']:
                    print(f"  ✗ {result['file']}")
                    for error in result['errors']:
                        print(f"    - {error}")

        if args.verbose:
            print(f"\nWarnings:")
            for result in results['files']:
                if result['warnings']:
                    print(f"  ⚠ {result['file']}")
                    for warning in result['warnings']:
                        print(f"    - {warning}")

        sys.exit(0 if results['failed'] == 0 else 1)

    except Exception as e:
        logger.error(f"✗ Validation failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
