"""
claude/ must not import agents/ at module scope (#210).

`agents/__init__.py` eagerly imports `base_maker_agent`, which imports
`run_claude_code` back out of `claude.claude_integration`. So a module-scope
import in the other direction closes a cycle, and whichever side is reached
first dies:

    claude/claude_integration.py   from agents.non_retryable import ...
      -> agents/__init__.py        from .base_maker_agent import MakerAgent
      -> agents/base_maker_agent.py  from claude.claude_integration import
                                      run_claude_code   # not defined yet
    ImportError: cannot import name 'run_claude_code' from partially
    initialized module 'claude.claude_integration'

#199 added exactly that edge, and it killed scripts/analyze_codebase.py,
scripts/generate_artifacts.py and scripts/generate_strategy.py outright --
every one of them failed before parsing arguments, so no invocation worked.

It went unnoticed for a reason worth keeping in mind: main.py reaches agents/
FIRST, so the running orchestrator was completely healthy and the whole unit
suite was green. Import order was the only thing separating a working process
from a broken one, and nothing tested the order.

Hence two tests. The behavioural one catches the cycle; the structural one
catches the edge that causes it, and is the one that will still be here when
`run_claude_code` has been renamed.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_in_a_clean_interpreter(statement: str):
    """A FRESH interpreter, because this cannot be tested in-process.

    By the time this file runs, pytest has already imported half the tree --
    `agents` included -- so the cycle is primed and every import succeeds. The
    bug only exists for a process that reaches `claude` first, which means a
    subprocess that has imported nothing.
    """
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, '-c', statement],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=120,
        env=environment,
    )


class TestTheCycleIsGone:
    def test_claude_integration_imports_on_its_own(self):
        """The exact reproduction from the issue."""
        done = _import_in_a_clean_interpreter(
            'import claude.claude_integration; print("OK")'
        )

        assert done.returncode == 0, done.stderr[-2000:]
        assert 'OK' in done.stdout

    def test_importing_claude_first_then_agents_still_works(self):
        """Order must stop mattering in BOTH directions, not just the one the
        issue happened to report."""
        done = _import_in_a_clean_interpreter(
            'import claude.claude_integration; import agents; print("OK")'
        )

        assert done.returncode == 0, done.stderr[-2000:]

    @pytest.mark.parametrize('script', [
        'analyze_codebase', 'generate_artifacts', 'generate_strategy',
    ])
    def test_the_scripts_the_cycle_killed_can_be_imported(self, script):
        done = _import_in_a_clean_interpreter(f'import scripts.{script}; print("OK")')

        assert done.returncode == 0, (
            f"scripts/{script}.py cannot be imported:\n{done.stderr[-2000:]}"
        )


class TestTheEdgeThatCausedItCannotComeBack:
    """The durable half.

    The behavioural tests above pin the symptom, and a symptom can be fixed by
    accident and broken again by accident. This pins the rule: no module under
    claude/ may import from agents/ at module scope. A function-level import is
    fine and is what the rest of the codebase already does -- twelve call sites
    import NonRetryableAgentError inside the function that raises it.
    """

    @staticmethod
    def _module_scope_agents_imports(path: Path):
        tree = ast.parse(path.read_text())
        offenders = []
        for node in tree.body:          # module scope ONLY, deliberately
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] == 'agents':
                    offenders.append((node.lineno, f"from {node.module} import ..."))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] == 'agents':
                        offenders.append((node.lineno, f"import {alias.name}"))
        return offenders

    def test_no_module_under_claude_imports_agents_at_module_scope(self):
        found = {}
        for path in sorted((_REPO_ROOT / 'claude').rglob('*.py')):
            offenders = self._module_scope_agents_imports(path)
            if offenders:
                found[path.relative_to(_REPO_ROOT)] = offenders

        assert not found, (
            "claude/ imports agents/ at module scope, which closes the import "
            "cycle through agents/__init__.py -> base_maker_agent -> "
            "claude.claude_integration (#210):\n" + "\n".join(
                f"  {p}:{line}  {what}"
                for p, offenders in found.items() for line, what in offenders
            ) + "\n\nMove the import inside the function that needs it, or put "
                "the symbol somewhere neither package owns (utils/ is a "
                "namespace package, so importing from it runs no __init__)."
        )

    def test_the_detector_would_actually_catch_the_regression(self, tmp_path):
        """Guards that assert 'nothing found' pass just as happily when they
        look in the wrong place or parse nothing."""
        offending = tmp_path / 'offending.py'
        offending.write_text(
            "from agents.non_retryable import NonRetryableAgentError\n"
            "import agents\n"
        )
        assert len(self._module_scope_agents_imports(offending)) == 2

        innocent = tmp_path / 'innocent.py'
        innocent.write_text(
            "def f():\n"
            "    from agents.non_retryable import NonRetryableAgentError\n"
            "    return NonRetryableAgentError\n"
        )
        assert self._module_scope_agents_imports(innocent) == []
