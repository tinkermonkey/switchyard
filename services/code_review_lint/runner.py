import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import LintConfig
from .rules import LintViolation, check_anti_patterns, check_selector_registry, check_generated_types

logger = logging.getLogger(__name__)

RULE_TITLES = {
    'vacuous_assertion': 'Vacuous Assertions',
    'hardcoded_wait': 'Hardcoded Waits',
    'swallowed_catch': 'Swallowed Exception Handlers',
    'css_xpath_selector': 'CSS/XPath Selectors in Tests',
    'selector_registry_sync': 'Selector Registry Sync',
    'generated_type_hand_written': 'Hand-written Generated Types',
}


@dataclass
class LintFailure:
    violations: List[LintViolation]

    def format_comment(self) -> str:
        lines = [
            "## Mechanical Lint Failures\n",
            "The following issues were detected before LLM review. Fix these first — no reviewer call was made.\n",
        ]
        by_rule: dict = {}
        for v in self.violations:
            by_rule.setdefault(v.rule, []).append(v)
        for rule, vs in sorted(by_rule.items()):
            title = RULE_TITLES.get(rule, rule)
            lines.append(f"### {title}")
            for v in vs:
                lines.append(f"- `{v.file}:{v.line}` — {v.detail}")
            lines.append("")
        return "\n".join(lines)


@dataclass
class LintResult:
    passed: bool
    failure: Optional[LintFailure] = None


class CodeReviewLintRunner:
    def __init__(self, lint_config: LintConfig, project_dir: str):
        self.config = lint_config
        self.project_dir = Path(project_dir)

    def run(self, base_commit: str, change_manifest: str = "") -> LintResult:
        changed_files = self._get_changed_files(base_commit)
        if not changed_files:
            logger.info("Lint: no changed files, skipping")
            return LintResult(passed=True)

        violations: List[LintViolation] = []

        registry_content: Optional[str] = None
        if self.config.selector_registry_path:
            registry_content = self._read_file(self.config.selector_registry_path)
            if registry_content is None:
                logger.info(
                    "Lint: selector registry not found at %s, skipping check",
                    self.config.selector_registry_path,
                )

        for file_path in changed_files:
            content = self._read_file(file_path)
            if content is None:
                continue

            if self.config.anti_patterns:
                violations.extend(check_anti_patterns(file_path, content))

            if self.config.selector_registry_path and registry_content is not None:
                violations.extend(check_selector_registry(file_path, content, registry_content))

            if self.config.generated_types_paths:
                violations.extend(check_generated_types(file_path, content, self.config.generated_types_paths))

        if violations:
            return LintResult(passed=False, failure=LintFailure(violations=violations))
        return LintResult(passed=True)

    def _get_changed_files(self, base_commit: str) -> List[str]:
        try:
            result = subprocess.run(
                ['git', 'diff', '--name-only', base_commit, 'HEAD'],
                cwd=str(self.project_dir),
                capture_output=True, text=True, check=True,
            )
            return [f.strip() for f in result.stdout.splitlines() if f.strip()]
        except Exception as e:
            logger.warning("Lint: could not get changed files: %s", e)
            return []

    def _read_file(self, relative_path: str) -> Optional[str]:
        path = self.project_dir / relative_path
        if not path.exists():
            return None
        try:
            return path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            logger.warning("Lint: could not read %s: %s", relative_path, e)
            return None
