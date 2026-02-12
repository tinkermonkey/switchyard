#!/usr/bin/env python3
"""
Codebase Analyzer

Fast, deterministic analysis of project structure without LLM calls.
Analyzes directory structure, tech stacks, dependencies, and samples key files.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configure logging
logger = logging.getLogger(__name__)

# Priority scoring for file sampling
FILE_PRIORITY_PATTERNS = {
    # Priority 100: Base classes, interfaces, __init__.py
    r'(base|abstract|interface)[_\-].*\.(py|ts|js|go|rs)$': 100,
    r'__init__\.py$': 100,
    r'index\.(ts|js)$': 100,

    # Priority 80: Entry points
    r'(main|app|server|index)\.(py|ts|js|go|rs)$': 80,
    r'(routes|router|endpoints?)\.(py|ts|js)$': 80,

    # Priority 70: Type definitions and models
    r'(models?|types?|schemas?|entities)\.(py|ts|js)$': 70,
    r'\.d\.ts$': 70,

    # Priority 60: Configuration files
    r'config\.(py|ts|js|yaml|yml|toml)$': 60,
    r'settings\.(py|ts|js)$': 60,

    # Priority 50: Core business logic
    r'(service|handler|controller|manager)\.(py|ts|js|go|rs)$': 50,

    # Priority 40: Utilities and helpers
    r'(util|helper|common)\.(py|ts|js|go|rs)$': 40,

    # Priority 30: Tests
    r'test_.*\.py$': 30,
    r'.*\.test\.(ts|js)$': 30,
    r'.*\.spec\.(ts|js)$': 30,
}


def calculate_file_priority(file_path: Path) -> int:
    """
    Calculate priority score for a file based on patterns

    Args:
        file_path: Path to file

    Returns:
        Priority score (higher = more important)
    """
    file_name = file_path.name.lower()

    for pattern, priority in FILE_PRIORITY_PATTERNS.items():
        if re.search(pattern, file_name):
            return priority

    # Default priority based on file type
    if file_path.suffix in ['.py', '.ts', '.js', '.go', '.rs']:
        return 20

    return 10


def analyze_directory_structure(project_dir: Path, max_depth: int = 3) -> Dict[str, Any]:
    """
    Analyze directory structure (top N levels)

    Args:
        project_dir: Project directory path
        max_depth: Maximum depth to analyze

    Returns:
        Directory structure analysis
    """
    total_files = 0
    total_dirs = 0
    structure = {}
    detected_layers = []
    test_directories = []

    # Common architectural layer indicators
    layer_indicators = [
        'api', 'apis', 'routes', 'endpoints',
        'services', 'service',
        'models', 'model', 'entities',
        'controllers', 'handlers',
        'middleware',
        'utils', 'utilities', 'helpers',
        'config', 'configuration',
        'core', 'common',
        'database', 'db',
        'schemas', 'types',
    ]

    # Test directory indicators
    test_indicators = ['test', 'tests', '__tests__', 'spec', 'specs']

    def scan_dir(path: Path, depth: int = 0):
        nonlocal total_files, total_dirs

        if depth > max_depth:
            return None

        result = {'name': path.name, 'children': []}

        try:
            for item in sorted(path.iterdir()):
                # Skip hidden directories and common ignores
                if item.name.startswith('.') or item.name in ['node_modules', '__pycache__', 'venv', 'dist', 'build']:
                    continue

                if item.is_dir():
                    total_dirs += 1

                    # Check for architectural layers
                    if depth <= 1 and item.name.lower() in layer_indicators:
                        detected_layers.append(f"{item.name}/")

                    # Check for test directories
                    if any(ind in item.name.lower() for ind in test_indicators):
                        test_directories.append(str(item.relative_to(project_dir)))

                    child = scan_dir(item, depth + 1)
                    if child:
                        result['children'].append(child)
                elif item.is_file():
                    total_files += 1
        except PermissionError:
            logger.warning(f"Permission denied: {path}")

        return result

    structure = scan_dir(project_dir)

    return {
        'total_files': total_files,
        'total_dirs': total_dirs,
        'structure': structure,
        'detected_layers': detected_layers,
        'test_directories': test_directories,
    }


def detect_tech_stacks(project_dir: Path) -> Dict[str, Any]:
    """
    Detect tech stacks from dependency files

    Args:
        project_dir: Project directory path

    Returns:
        Tech stack detection results
    """
    languages = []
    frameworks = []
    primary_language = None

    # Python detection
    if (project_dir / 'requirements.txt').exists() or (project_dir / 'pyproject.toml').exists():
        languages.append('python')
        primary_language = primary_language or 'python'

        # Detect Python frameworks
        python_frameworks = detect_python_frameworks(project_dir)
        frameworks.extend(python_frameworks)

    # Node.js detection
    if (project_dir / 'package.json').exists():
        languages.append('javascript')
        primary_language = primary_language or 'javascript'

        # Detect Node.js frameworks
        node_frameworks = detect_node_frameworks(project_dir)
        frameworks.extend(node_frameworks)

    # Rust detection
    if (project_dir / 'Cargo.toml').exists():
        languages.append('rust')
        primary_language = primary_language or 'rust'

    # Go detection
    if (project_dir / 'go.mod').exists():
        languages.append('go')
        primary_language = primary_language or 'go'

    return {
        'languages': languages,
        'frameworks': frameworks,
        'primary_language': primary_language or 'unknown',
    }


def detect_python_frameworks(project_dir: Path) -> List[str]:
    """Detect Python frameworks from requirements"""
    frameworks = []

    requirements_file = project_dir / 'requirements.txt'
    pyproject_file = project_dir / 'pyproject.toml'

    framework_indicators = {
        'fastapi': ['fastapi'],
        'django': ['django'],
        'flask': ['flask'],
        'pytest': ['pytest'],
        'pydantic': ['pydantic'],
    }

    content = ""

    if requirements_file.exists():
        try:
            with open(requirements_file, 'r') as f:
                content += f.read().lower()
        except Exception as e:
            logger.warning(f"Failed to read requirements.txt: {e}")

    if pyproject_file.exists():
        try:
            with open(pyproject_file, 'r') as f:
                content += f.read().lower()
        except Exception as e:
            logger.warning(f"Failed to read pyproject.toml: {e}")

    for framework, indicators in framework_indicators.items():
        if any(ind in content for ind in indicators):
            frameworks.append(framework)

    return frameworks


def detect_node_frameworks(project_dir: Path) -> List[str]:
    """Detect Node.js frameworks from package.json"""
    frameworks = []

    package_json = project_dir / 'package.json'

    if not package_json.exists():
        return frameworks

    try:
        with open(package_json, 'r') as f:
            import json
            data = json.load(f)

        deps = {}
        deps.update(data.get('dependencies', {}))
        deps.update(data.get('devDependencies', {}))

        framework_indicators = {
            'react': ['react'],
            'vue': ['vue'],
            'angular': ['@angular/core'],
            'express': ['express'],
            'next.js': ['next'],
            'nest.js': ['@nestjs/core'],
        }

        for framework, indicators in framework_indicators.items():
            if any(ind in deps for ind in indicators):
                frameworks.append(framework)

    except Exception as e:
        logger.warning(f"Failed to parse package.json: {e}")

    return frameworks


def parse_dependencies(project_dir: Path) -> Dict[str, List[str]]:
    """
    Parse dependency files

    Args:
        project_dir: Project directory path

    Returns:
        Dependencies by language
    """
    dependencies = {}

    # Python dependencies
    requirements_file = project_dir / 'requirements.txt'
    if requirements_file.exists():
        try:
            with open(requirements_file, 'r') as f:
                python_deps = [
                    line.strip() for line in f
                    if line.strip() and not line.startswith('#')
                ]
                dependencies['python'] = python_deps[:50]  # Limit to 50
        except Exception as e:
            logger.warning(f"Failed to parse requirements.txt: {e}")

    # Node.js dependencies
    package_json = project_dir / 'package.json'
    if package_json.exists():
        try:
            with open(package_json, 'r') as f:
                import json
                data = json.load(f)

            all_deps = {}
            all_deps.update(data.get('dependencies', {}))
            all_deps.update(data.get('devDependencies', {}))

            node_deps = [f"{k}@{v}" for k, v in list(all_deps.items())[:50]]
            dependencies['node'] = node_deps
        except Exception as e:
            logger.warning(f"Failed to parse package.json: {e}")

    return dependencies


def identify_critical_dependencies(dependencies: Dict[str, List[str]]) -> List[str]:
    """
    Identify critical dependencies (frameworks, not utilities)

    Args:
        dependencies: Dependencies by language

    Returns:
        List of critical dependency names
    """
    critical = []

    # Framework keywords (priority order)
    framework_keywords = [
        'fastapi', 'django', 'flask',
        'react', 'vue', 'angular', 'next',
        'express', 'nest',
        'pytest', 'jest',
        'postgresql', 'mysql', 'mongodb', 'redis',
        'elasticsearch', 'kafka',
        'pydantic', 'sqlalchemy',
    ]

    for lang, deps in dependencies.items():
        for dep in deps:
            dep_lower = dep.split('@')[0].split('==')[0].split('>=')[0].lower()

            for keyword in framework_keywords:
                if keyword in dep_lower:
                    critical.append(dep_lower)
                    break

    return list(set(critical))  # Remove duplicates


def analyze_test_structure(project_dir: Path) -> Dict[str, Any]:
    """
    Analyze test structure

    Args:
        project_dir: Project directory path

    Returns:
        Test structure analysis
    """
    test_framework = None
    test_count = 0
    test_directories = []

    # Detect test framework
    if (project_dir / 'pytest.ini').exists() or (project_dir / 'pyproject.toml').exists():
        # Check for pytest in requirements
        requirements_file = project_dir / 'requirements.txt'
        if requirements_file.exists():
            try:
                with open(requirements_file, 'r') as f:
                    if 'pytest' in f.read().lower():
                        test_framework = 'pytest'
            except Exception:
                pass

    if (project_dir / 'jest.config.js').exists() or (project_dir / 'jest.config.ts').exists():
        test_framework = 'jest'

    # Find test files
    test_patterns = ['**/test_*.py', '**/*_test.py', '**/*.test.ts', '**/*.test.js', '**/*.spec.ts', '**/*.spec.js']

    for pattern in test_patterns:
        try:
            test_files = list(project_dir.glob(pattern))
            test_count += len(test_files)

            # Collect unique test directories
            for test_file in test_files:
                test_dir = str(test_file.parent.relative_to(project_dir))
                if test_dir not in test_directories:
                    test_directories.append(test_dir)
        except Exception as e:
            logger.warning(f"Failed to glob {pattern}: {e}")

    return {
        'test_framework': test_framework or 'unknown',
        'test_count': test_count,
        'test_directories': test_directories,
    }


def detect_deployment_patterns(project_dir: Path) -> Dict[str, Any]:
    """
    Detect deployment configuration

    Args:
        project_dir: Project directory path

    Returns:
        Deployment pattern analysis
    """
    docker = False
    ci_cd = None
    deployment_files = []

    # Docker detection
    if (project_dir / 'Dockerfile').exists():
        docker = True
        deployment_files.append('Dockerfile')

    if (project_dir / 'docker-compose.yml').exists() or (project_dir / 'docker-compose.yaml').exists():
        docker = True
        deployment_files.append('docker-compose.yml')

    # CI/CD detection
    if (project_dir / '.github' / 'workflows').exists():
        ci_cd = 'github_actions'
        try:
            workflow_files = list((project_dir / '.github' / 'workflows').glob('*.yml'))
            deployment_files.extend([f'.github/workflows/{f.name}' for f in workflow_files])
        except Exception:
            pass

    if (project_dir / '.gitlab-ci.yml').exists():
        ci_cd = 'gitlab_ci'
        deployment_files.append('.gitlab-ci.yml')

    if (project_dir / 'Makefile').exists():
        deployment_files.append('Makefile')

    return {
        'docker': docker,
        'ci_cd': ci_cd or 'none',
        'deployment_files': deployment_files,
    }


def sample_key_files(project_dir: Path, max_files: int = 20) -> List[Dict[str, Any]]:
    """
    Smart sampling of key files

    Args:
        project_dir: Project directory path
        max_files: Maximum number of files to sample

    Returns:
        List of sampled file info with priority scores
    """
    candidates = []

    # Scan for Python, TypeScript, JavaScript, Go, Rust files
    extensions = ['.py', '.ts', '.js', '.go', '.rs']

    for ext in extensions:
        try:
            for file_path in project_dir.rglob(f'*{ext}'):
                # Skip excluded directories
                if any(excl in file_path.parts for excl in ['.git', 'node_modules', '__pycache__', 'venv', 'dist', 'build', '.pytest_cache']):
                    continue

                # Calculate priority
                priority = calculate_file_priority(file_path)

                try:
                    size = file_path.stat().st_size

                    # Skip very large files
                    if size > 100000:  # 100KB
                        continue

                    candidates.append({
                        'path': str(file_path.relative_to(project_dir)),
                        'size': size,
                        'priority': priority,
                    })
                except Exception as e:
                    logger.warning(f"Failed to stat {file_path}: {e}")
        except Exception as e:
            logger.warning(f"Failed to glob {ext}: {e}")

    # Sort by priority (descending), then by size (ascending for same priority)
    candidates.sort(key=lambda x: (-x['priority'], x['size']))

    # Return top N
    return candidates[:max_files]


def run_codebase_analysis(project: str, workspace_root: Path) -> Dict[str, Any]:
    """
    Main analysis orchestrator

    Args:
        project: Project name
        workspace_root: Workspace root path

    Returns:
        Complete codebase analysis
    """
    project_dir = workspace_root / project

    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    logger.info(f"Analyzing codebase: {project}")

    # Phase 1: Discovery
    logger.info("  Phase 1: Analyzing directory structure...")
    structure = analyze_directory_structure(project_dir)
    logger.info(f"    Found {structure['total_files']} files, {structure['total_dirs']} directories")

    logger.info("  Phase 2: Detecting tech stacks...")
    tech_stacks = detect_tech_stacks(project_dir)
    logger.info(f"    Primary language: {tech_stacks['primary_language']}")
    logger.info(f"    Frameworks: {', '.join(tech_stacks['frameworks']) or 'None detected'}")

    # Phase 2: Dependencies
    logger.info("  Phase 3: Parsing dependencies...")
    dependencies = parse_dependencies(project_dir)
    dep_count = sum(len(deps) for deps in dependencies.values())
    logger.info(f"    Found {dep_count} dependencies")

    critical_deps = identify_critical_dependencies(dependencies)
    logger.info(f"    Critical dependencies: {', '.join(critical_deps) or 'None'}")

    # Phase 3: Code patterns
    logger.info("  Phase 4: Analyzing test structure...")
    test_structure = analyze_test_structure(project_dir)
    logger.info(f"    Test framework: {test_structure['test_framework']}")
    logger.info(f"    Test count: {test_structure['test_count']}")

    logger.info("  Phase 5: Detecting deployment patterns...")
    deployment = detect_deployment_patterns(project_dir)
    logger.info(f"    Docker: {deployment['docker']}")
    logger.info(f"    CI/CD: {deployment['ci_cd']}")

    # Phase 4: Smart sampling
    logger.info("  Phase 6: Sampling key files...")
    key_files = sample_key_files(project_dir, max_files=20)
    logger.info(f"    Sampled {len(key_files)} key files")

    # Phase 5: Save to JSON
    analysis = {
        'project': project,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'structure': structure,
        'tech_stacks': tech_stacks,
        'dependencies': {
            'all': dependencies,
            'critical': critical_deps
        },
        'testing': test_structure,
        'deployment': deployment,
        'key_files': key_files
    }

    # Save analysis
    output_dir = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / 'state' / 'projects' / project
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / 'codebase_analysis.json'

    with open(output_file, 'w') as f:
        json.dump(analysis, f, indent=2)

    logger.info(f"  ✓ Analysis saved to: {output_file}")

    return analysis


def main():
    """CLI entry point for testing"""
    import argparse

    parser = argparse.ArgumentParser(description="Analyze project codebase")
    parser.add_argument('project', help='Project name to analyze')
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # Get workspace root
    if Path('/workspace').exists():
        workspace_root = Path('/workspace')
    else:
        workspace_root = Path(__file__).parent.parent.parent

    try:
        analysis = run_codebase_analysis(args.project, workspace_root)
        print(f"\n✓ Analysis complete for {args.project}")
        print(f"  Languages: {', '.join(analysis['tech_stacks']['languages'])}")
        print(f"  Frameworks: {', '.join(analysis['tech_stacks']['frameworks']) or 'None'}")
        print(f"  Files analyzed: {analysis['structure']['total_files']}")
        print(f"  Key files sampled: {len(analysis['key_files'])}")
    except Exception as e:
        logger.error(f"✗ Analysis failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
