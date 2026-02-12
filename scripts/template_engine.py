#!/usr/bin/env python3
"""
Template Engine

Loads and populates markdown templates for agent and skill generation.
"""

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configure logging
logger = logging.getLogger(__name__)


def load_template(template_name: str) -> str:
    """
    Load template from scripts/templates/

    Args:
        template_name: Template filename (e.g., 'agent_template.md')

    Returns:
        Template content as string
    """
    template_path = Path(__file__).parent / 'templates' / template_name

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    with open(template_path, 'r') as f:
        return f.read()


def populate_template(template: str, context: Dict[str, Any]) -> str:
    """
    Replace {placeholders} with context values

    Args:
        template: Template string with {placeholders}
        context: Dictionary of values to substitute

    Returns:
        Populated template string
    """
    result = template

    # Track which placeholders were filled
    filled_placeholders = set()

    for key, value in context.items():
        placeholder = f"{{{key}}}"
        if placeholder in result:
            result = result.replace(placeholder, str(value))
            filled_placeholders.add(placeholder)

    # Only remove unfilled template placeholders (not all curly braces)
    # Extract all {word} patterns from original template
    template_placeholders = set(re.findall(r'\{[a-zA-Z_][a-zA-Z0-9_]*\}', template))

    # Remove only the unfilled template placeholders
    for placeholder in template_placeholders:
        if placeholder not in filled_placeholders:
            result = result.replace(placeholder, '')

    return result


def _infer_architecture_style(layers: List[str]) -> str:
    """
    Infer architecture style from detected layers

    Args:
        layers: List of detected layer directories

    Returns:
        Architecture style description
    """
    layers_lower = [l.lower() for l in layers]

    if any('api' in l or 'routes' in l or 'endpoints' in l for l in layers_lower):
        return "Multi-tier (API-driven)"
    elif any('services' in l or 'service' in l for l in layers_lower):
        return "Service-oriented"
    elif any('models' in l or 'entities' in l for l in layers_lower):
        return "Domain-driven"
    else:
        return "Modular"


def _build_capabilities(agent_spec: Dict[str, Any]) -> str:
    """
    Build capabilities section for agent

    Args:
        agent_spec: Agent specification from strategy

    Returns:
        Formatted capabilities section
    """
    tools = agent_spec.get('tools', [])

    capabilities = []

    if 'Read' in tools or 'Grep' in tools or 'Glob' in tools:
        capabilities.append("- **Code Analysis**: Read and analyze project files")

    if 'Edit' in tools or 'Write' in tools:
        capabilities.append("- **Code Modification**: Make changes to the codebase")

    if 'Bash' in tools:
        capabilities.append("- **Command Execution**: Run build, test, and deployment commands")

    if 'WebSearch' in tools or 'WebFetch' in tools:
        capabilities.append("- **Research**: Look up documentation and best practices")

    if 'Task' in tools:
        capabilities.append("- **Delegation**: Coordinate with other agents for complex workflows")

    # Add purpose-specific capabilities
    purpose_lower = agent_spec.get('purpose', '').lower()

    if 'architect' in purpose_lower:
        capabilities.append("- **Architecture Guidance**: Explain design patterns and component relationships")

    if 'guardian' in purpose_lower or 'enforce' in purpose_lower:
        capabilities.append("- **Standards Enforcement**: Ensure code follows project conventions")

    if 'test' in purpose_lower:
        capabilities.append("- **Testing**: Run tests and analyze test results")

    if 'deploy' in purpose_lower:
        capabilities.append("- **Deployment**: Manage deployment processes and configurations")

    if 'doc' in purpose_lower:
        capabilities.append("- **Documentation**: Maintain and update project documentation")

    if 'api' in purpose_lower:
        capabilities.append("- **API Expertise**: Work with API endpoints and integrations")

    if 'data' in purpose_lower or 'database' in purpose_lower:
        capabilities.append("- **Data Management**: Work with databases and data models")

    return '\n'.join(capabilities) if capabilities else '- General project assistance'


def _build_guidelines(project: str, analysis: Dict[str, Any]) -> str:
    """
    Build guidelines section for agent

    Args:
        project: Project name
        analysis: Codebase analysis

    Returns:
        Formatted guidelines section
    """
    guidelines = []

    # Test-related guidelines
    if analysis['testing']['test_count'] > 0:
        test_framework = analysis['testing']['test_framework']
        guidelines.append(f"- **Testing**: Use {test_framework} for running tests")

    # Deployment guidelines
    if analysis['deployment']['docker']:
        guidelines.append("- **Docker**: Project uses Docker for containerization")

    if analysis['deployment']['ci_cd'] != 'none':
        ci_cd = analysis['deployment']['ci_cd'].replace('_', ' ').title()
        guidelines.append(f"- **CI/CD**: Project uses {ci_cd} for automation")

    # Code style
    primary_lang = analysis['tech_stacks']['primary_language']
    if primary_lang == 'python':
        guidelines.append("- **Code Style**: Follow PEP 8 conventions for Python code")
    elif primary_lang == 'javascript':
        guidelines.append("- **Code Style**: Follow project's ESLint configuration")

    # Architecture
    guidelines.append(f"- **Architecture**: Respect the project's modular structure")
    guidelines.append("- **Documentation**: Update relevant documentation when making changes")

    return '\n'.join(guidelines) if guidelines else '- Follow project conventions and best practices'


def _build_common_tasks(agent_spec: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    """
    Build common tasks section for agent

    Args:
        agent_spec: Agent specification
        analysis: Codebase analysis

    Returns:
        Formatted common tasks section
    """
    purpose_lower = agent_spec.get('purpose', '').lower()
    tasks = []

    if 'architect' in purpose_lower:
        tasks.append("- Explain how a specific component works")
        tasks.append("- Review architectural decisions")
        tasks.append("- Suggest improvements to system design")

    if 'guardian' in purpose_lower or 'enforce' in purpose_lower:
        tasks.append("- Review code for standards compliance")
        tasks.append("- Identify potential antipatterns")
        tasks.append("- Enforce architectural boundaries")

    if 'test' in purpose_lower:
        if analysis['testing']['test_count'] > 0:
            tasks.append(f"- Run tests using {analysis['testing']['test_framework']}")
        tasks.append("- Analyze test coverage")
        tasks.append("- Debug failing tests")

    if 'deploy' in purpose_lower:
        if analysis['deployment']['docker']:
            tasks.append("- Build Docker images")
        tasks.append("- Manage deployment configurations")
        tasks.append("- Troubleshoot deployment issues")

    if 'doc' in purpose_lower:
        tasks.append("- Update README.md")
        tasks.append("- Generate API documentation")
        tasks.append("- Create usage examples")

    if 'api' in purpose_lower:
        tasks.append("- Document API endpoints")
        tasks.append("- Test API functionality")
        tasks.append("- Debug API issues")

    if 'data' in purpose_lower or 'database' in purpose_lower:
        tasks.append("- Design data models")
        tasks.append("- Write database migrations")
        tasks.append("- Optimize queries")

    # Generic fallback
    if not tasks:
        tasks.append("- Assist with project-specific development tasks")
        tasks.append("- Answer questions about the codebase")

    return '\n'.join(tasks)


def build_agent_context(
    project: str,
    agent_spec: Dict[str, Any],
    analysis: Dict[str, Any],
    strategy: Dict[str, Any],
    codebase_hash: str
) -> Dict[str, str]:
    """
    Build context dict for agent template

    Args:
        project: Project name
        agent_spec: Agent specification from strategy
        analysis: Codebase analysis
        strategy: Full strategy
        codebase_hash: Hash of codebase

    Returns:
        Context dictionary for template population
    """
    # Extract from analysis
    tech_stack = ', '.join(analysis['tech_stacks']['languages'])
    frameworks = ', '.join(analysis['tech_stacks']['frameworks']) if analysis['tech_stacks']['frameworks'] else 'None'

    # Build architecture details from structure
    layers = analysis['structure'].get('detected_layers', [])
    if layers:
        architecture_details = "**Detected Layers:**\n" + '\n'.join([f"- `{layer}`" for layer in layers])
    else:
        architecture_details = "*No explicit layering detected - modular structure*"

    # Extract key components from sampled files
    key_files = analysis['key_files'][:10]  # Top 10
    if key_files:
        key_components = '\n'.join([f"- `{f['path']}`" for f in key_files])
    else:
        key_components = "*Key files will be identified during usage*"

    # Build context
    return {
        'agent_name': agent_spec['name'],
        'description': agent_spec['purpose'],
        'tools': ', '.join(agent_spec.get('tools', ['Bash', 'Read', 'Grep', 'Glob'])),
        'model': agent_spec.get('model', 'sonnet'),
        'color': agent_spec.get('color', 'blue'),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'project': project,
        'hash': codebase_hash,
        'display_name': agent_spec['name'].replace('-', ' ').title(),
        'role_description': agent_spec.get('rationale', agent_spec['purpose']),
        'tech_stack': tech_stack,
        'frameworks': frameworks,
        'architecture_style': _infer_architecture_style(layers),
        'architecture_details': architecture_details,
        'key_components': key_components,
        'capabilities': _build_capabilities(agent_spec),
        'guidelines': _build_guidelines(project, analysis),
        'common_tasks': _build_common_tasks(agent_spec, analysis)
    }


def _build_skill_implementation(skill_spec: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    """
    Build implementation steps for skill

    Args:
        skill_spec: Skill specification
        analysis: Codebase analysis

    Returns:
        Formatted implementation steps
    """
    purpose_lower = skill_spec.get('purpose', '').lower()
    steps = []

    if 'architecture' in purpose_lower:
        steps.append("1. Display high-level architecture overview")
        steps.append("2. Show key components and their relationships")
        steps.append("3. Explain architectural patterns in use")

    elif 'test' in purpose_lower:
        if analysis['testing']['test_framework'] != 'unknown':
            test_framework = analysis['testing']['test_framework']
            steps.append(f"1. Run `{test_framework}` command")
            steps.append("2. Display test results")
            steps.append("3. Highlight failures and errors")

    elif 'deploy' in purpose_lower:
        if analysis['deployment']['docker']:
            steps.append("1. Build Docker image")
            steps.append("2. Run deployment checks")
            steps.append("3. Execute deployment procedure")

    # Generic fallback
    if not steps:
        steps.append("1. Execute skill-specific operations")
        steps.append("2. Display results to user")

    return '\n'.join(steps)


def _build_skill_examples(skill_spec: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    """
    Build examples section for skill

    Args:
        skill_spec: Skill specification
        analysis: Codebase analysis

    Returns:
        Formatted examples section
    """
    skill_name = skill_spec['name']
    purpose_lower = skill_spec.get('purpose', '').lower()
    examples = []

    if 'architecture' in purpose_lower:
        examples.append(f"**Show architecture overview:**")
        examples.append(f"```bash")
        examples.append(f"/{skill_name}")
        examples.append(f"```")

    elif 'test' in purpose_lower:
        examples.append(f"**Run all tests:**")
        examples.append(f"```bash")
        examples.append(f"/{skill_name}")
        examples.append(f"```")
        if skill_spec.get('args'):
            examples.append(f"\n**Run specific test:**")
            examples.append(f"```bash")
            examples.append(f"/{skill_name} test_name")
            examples.append(f"```")

    elif 'deploy' in purpose_lower:
        examples.append(f"**Run deployment:**")
        examples.append(f"```bash")
        examples.append(f"/{skill_name}")
        examples.append(f"```")

    # Generic fallback
    if not examples:
        examples.append(f"**Basic usage:**")
        examples.append(f"```bash")
        examples.append(f"/{skill_name}")
        examples.append(f"```")

    return '\n'.join(examples)


def build_skill_context(
    project: str,
    skill_spec: Dict[str, Any],
    analysis: Dict[str, Any],
    strategy: Dict[str, Any],
    codebase_hash: str
) -> Dict[str, str]:
    """
    Build context dict for skill template

    Args:
        project: Project name
        skill_spec: Skill specification from strategy
        analysis: Codebase analysis
        strategy: Full strategy
        codebase_hash: Hash of codebase

    Returns:
        Context dictionary for template population
    """
    args = skill_spec.get('args', '')
    args_example = f" {args}" if args else ""

    return {
        'skill_name': skill_spec['name'],
        'description': skill_spec['purpose'],
        'args': args,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'project': project,
        'hash': codebase_hash,
        'display_name': skill_spec['name'].replace('-', ' ').title(),
        'purpose': skill_spec.get('rationale', skill_spec['purpose']),
        'args_example': args_example,
        'implementation_steps': _build_skill_implementation(skill_spec, analysis),
        'examples': _build_skill_examples(skill_spec, analysis),
        'notes': f"This skill is specific to the {project} project."
    }
