"""No state path may be derived from where the code happens to live (#181).

The documented way to run this suite is `pytest tests/unit` from the
repository root. On the deployment that root IS the directory bind-mounted at
`/app`, so any module resolving its state directory from
`Path(__file__).parent.parent` wrote the suite's fixtures into the LIVE state
tree -- and the production watchdog then did real work on them. Seventeen files
were observed reappearing after a verified-clean deletion, every one
timestamped to a test run rather than to the orchestrator.

Eight modules already read ORCHESTRATOR_ROOT. Two did not, and those two were
the hole.
"""

import importlib
import os
from pathlib import Path

import pytest

from config.state_manager import orchestrator_state_root


class TestTheResolver:

    def test_orchestrator_root_wins_when_set(self, monkeypatch):
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '/scratch/elsewhere')
        assert orchestrator_state_root() == Path('/scratch/elsewhere/state')

    def test_it_falls_back_to_the_checkout_when_unset(self, monkeypatch):
        """Unchanged production behaviour: with nothing set, the deployment
        still resolves its own `state/`. The fix adds an override, it does not
        move anything by default."""
        monkeypatch.delenv('ORCHESTRATOR_ROOT', raising=False)
        import config.state_manager as sm

        expected = Path(sm.__file__).parent.parent / 'state'
        assert orchestrator_state_root() == expected

    def test_an_empty_value_is_treated_as_unset(self, monkeypatch):
        """`-e ORCHESTRATOR_ROOT=` on a docker run passes an empty string.
        Reading that as a root would resolve every state path to `/state`."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '')
        import config.state_manager as sm

        assert orchestrator_state_root() == Path(sm.__file__).parent.parent / 'state'


class TestBothHoldoutsUseIt:
    """The two modules that derived from __file__ instead."""

    def test_github_state_manager_follows_the_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        import config.state_manager as sm
        importlib.reload(sm)

        manager = sm.GitHubStateManager()

        assert manager.state_root == tmp_path / 'state'
        assert manager.projects_state_dir == tmp_path / 'state' / 'projects'

    def test_pr_review_state_manager_follows_the_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        import state_management.pr_review_state_manager as prs
        importlib.reload(prs)

        manager = prs.PRReviewStateManager()

        assert manager.state_root == tmp_path / 'state' / 'projects'

    def test_an_explicit_state_root_still_beats_the_environment(self, monkeypatch, tmp_path):
        """Callers that pass a root mean it -- scripts/dry_run_state_sweep.py
        depends on this."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '/ignored')
        import config.state_manager as sm

        manager = sm.GitHubStateManager(state_root=str(tmp_path / 'chosen'))

        assert manager.state_root == tmp_path / 'chosen'


class TestNoModuleStillDerivesStateFromItsOwnLocation:

    def test_no_first_party_module_builds_a_state_path_from___file__(self):
        """The guard. A new `Path(__file__).parent.parent / "state"` anywhere
        reopens #181, and it will not be noticed until something deletes or
        rewrites production data.

        config/state_manager.py is exempt because its fallback IS the
        documented production behaviour -- it is reached only when
        ORCHESTRATOR_ROOT is unset, which is the deployment's own case.
        """
        import re

        root = Path(__file__).parent.parent.parent
        exempt = {'config/state_manager.py'}
        pattern = re.compile(
            r'Path\(__file__\)\.parent\.parent\s*/\s*["\']state["\']'
        )

        offenders = []
        for source in root.rglob('*.py'):
            relative = source.relative_to(root)
            if relative.parts[0] in ('tests', '.claude', 'node_modules', 'venv', '.venv'):
                continue
            if str(relative) in exempt:
                continue
            if pattern.search(source.read_text(errors='ignore')):
                offenders.append(str(relative))

        assert offenders == [], (
            f"these modules derive a state path from their own location: "
            f"{offenders}. Use config.state_manager.orchestrator_state_root()."
        )


class TestTheSuiteCannotReachTheDeploymentsState:

    def test_the_active_root_is_never_the_bind_mounted_app_directory(self):
        """End to end: whatever this run resolved, it is not /app.

        /app is the orchestrator's live code and state on the deployment. This
        asserts the property that actually matters, rather than asserting that
        one particular mechanism is in place.
        """
        active = os.environ.get('ORCHESTRATOR_ROOT')

        assert active, "conftest must have redirected ORCHESTRATOR_ROOT"
        assert Path(active) != Path('/app')
        assert orchestrator_state_root() != Path('/app/state')
