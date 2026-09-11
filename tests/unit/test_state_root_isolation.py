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
        """Constructed directly, NOT via a module reload.

        An earlier version called importlib.reload here.
        `config/state_manager.py` ends with `state_manager = GitHubStateManager()`
        at module scope, so a reload rebinds that process-wide singleton to a
        root under tmp_path -- which pytest then deletes -- and mints a second
        class object, so `isinstance` and
        `patch('config.state_manager.GitHubStateManager')` in any later test
        stop referring to what their consumers actually use. Roughly 20 call
        sites do a function-level `from config.state_manager import
        state_manager` and would pick up the new one while module-level
        importers kept the old.

        It was never needed: orchestrator_state_root() reads the environment on
        every call, so constructing under the patched env proves the same thing
        with no session-wide side effect.
        """
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        import config.state_manager as sm

        manager = sm.GitHubStateManager()

        assert manager.state_root == tmp_path / 'state'
        assert manager.projects_state_dir == tmp_path / 'state' / 'projects'

    def test_pr_review_state_manager_follows_the_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        import state_management.pr_review_state_manager as prs

        manager = prs.PRReviewStateManager()

        assert manager.state_root == tmp_path / 'state' / 'projects'

    def test_the_module_level_singleton_honours_the_override_too(self):
        """The singleton is the call site that actually matters.

        `config/state_manager.py` builds one at import time, and
        services/pipeline_progression.py, claude/docker_runner.py,
        agents/orchestrator_integration.py and main.py all reach for it. Both
        tests above construct fresh managers, which would pass even if the
        singleton had been built before the redirect took effect.
        """
        import config.state_manager as sm

        assert sm.state_manager.state_root == orchestrator_state_root(), (
            f"singleton={sm.state_manager.state_root} "
            f"resolver={orchestrator_state_root()} "
            f"env={os.environ.get('ORCHESTRATOR_ROOT')!r}"
        )

    def test_an_explicit_state_root_still_beats_the_environment(self, monkeypatch, tmp_path):
        """Callers that pass a root mean it.

        No non-test caller passes `state_root=` today --
        scripts/dry_run_state_sweep.py mutates the existing singleton in place
        instead, deliberately, because importers hold that object. The
        parameter is still part of the contract and two test files rely on it.
        """
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '/ignored')
        import config.state_manager as sm

        manager = sm.GitHubStateManager(state_root=str(tmp_path / 'chosen'))

        assert manager.state_root == tmp_path / 'chosen'


class TestNoModuleStillDerivesStateFromItsOwnLocation:

    def test_no_first_party_module_builds_a_state_path_from_its_own_location(self):
        """A tripwire for both shapes that caused #181.

        The first version of this matched one exact spelling,
        `Path(__file__).parent.parent / "state"`, and a reviewer defeated it
        with five one-line rewrites -- `parents[1]`, a module constant, an
        f-string, `os.path.dirname`, `.joinpath("state")` -- none of which it
        saw. It also could not see the other half of #181, which turned out to
        be the more common shape here: a relative or hardcoded literal that
        ignores ORCHESTRATOR_ROOT entirely. Five such sites existed while that
        scan was passing. Three were writers, and one mkdir'd in a constructor.

        So this matches on CO-OCCURRENCE rather than on syntax. It will
        overmatch eventually; that is the intended direction. Add to EXEMPT
        with a reason rather than narrowing the pattern.
        """
        import re

        root = Path(__file__).parent.parent.parent

        # path -> why it legitimately names one of these
        EXEMPT = {
            # Its fallback IS production behaviour: reached only when
            # ORCHESTRATOR_ROOT is unset, which is the deployment's own case.
            'config/state_manager.py': 'defines the resolver',
            # Names swept directories as data, not as paths it opens.
            'services/data_retention.py': 'retention rule table',
            # A separate service (switchyard-mcp) in its own container, with
            # its own APP_ROOT override and a /app default. Not in this
            # suite's import graph, and configurable rather than hardcoded.
            'mcp/server.py': 'separate service, APP_ROOT-configurable',
        }

        derived_from_file = re.compile(r'__file__.*\bstate\b|\bstate\b.*__file__')

        # Path-SHAPED literals only. An earlier attempt matched any quoted
        # "state" and drowned in dict keys (`"state": content.get("state")`),
        # `self.state`, and every status field in the codebase -- a guard that
        # cries wolf gets deleted, which is worse than one that overmatches a
        # little.
        literal_state_root = re.compile(
            # "state/..." or "/app/state/..." or "/workspace/switchyard/state/..."
            r'["\'](?:/workspace/switchyard|/app)?/?state/'
            # ...or a bare 'state' passed to a path constructor
            r'|(?:Path\(|os\.path\.join\()[^)]*["\']state["\']'
        )

        offenders = []
        for source in sorted(root.rglob('*.py')):
            relative = source.relative_to(root)
            if relative.parts[0] in ('tests', '.claude', 'node_modules', 'venv', '.venv'):
                continue
            if str(relative) in EXEMPT:
                continue
            for number, line in enumerate(source.read_text(errors='ignore').splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if derived_from_file.search(line) or literal_state_root.search(line):
                    offenders.append(f"{relative}:{number}: {stripped[:90]}")

        assert offenders == [], (
            "these lines build a state path from the code's own location or "
            "from a literal, either of which ignores ORCHESTRATOR_ROOT and "
            "writes into the deployment (#181):\n  " + "\n  ".join(offenders) +
            "\nUse config.state_manager.orchestrator_state_root()."
        )


class TestTheSuiteCannotReachTheDeploymentsState:

    def test_the_active_root_is_not_the_deployments_directory_under_any_alias(self):
        """Compare identity, not spelling.

        The first version asserted `Path(active) != Path('/app')`. That is not
        the property it claimed. On the deployment `/app` and
        `/workspace/switchyard` are the SAME INODE -- docker-compose mounts the
        checkout twice -- so it passed for `/workspace/switchyard`, for `.`,
        and for `/app/../app`, every one of which writes production.

        `orchestrator_state_root()` now refuses relative values outright, so
        what is left to check is the absolute aliases, and st_dev/st_ino is the
        only thing that answers that. Skips where /app does not exist, because
        off-container there is no deployment to collide with.
        """
        active = os.environ.get('ORCHESTRATOR_ROOT')
        assert active, "conftest must have redirected ORCHESTRATOR_ROOT"

        deployment = Path('/app')
        if not deployment.is_dir():
            pytest.skip("no /app on this host; nothing to collide with")

        def identity(path: Path):
            info = path.stat()
            return (info.st_dev, info.st_ino)

        assert identity(Path(active)) != identity(deployment), (
            f"ORCHESTRATOR_ROOT={active!r} is the deployment directory under "
            f"another name -- writing there is exactly #181"
        )

    def test_a_relative_root_is_refused_rather_than_resolved_against_the_cwd(
        self, monkeypatch
    ):
        """`-e ORCHESTRATOR_ROOT=tmp/rv200`, a dropped leading slash, is the
        likeliest typo in the documented command -- and it used to mean "write
        under the checkout"."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', 'relative-oops')

        with pytest.raises(ValueError, match='absolute'):
            orchestrator_state_root()

    def test_whitespace_is_treated_as_unset(self, monkeypatch):
        """`-e ORCHESTRATOR_ROOT=` and a stray space are the same accident."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '   ')
        import config.state_manager as sm

        assert orchestrator_state_root() == (
            Path(sm.__file__).parent.parent / 'state'
        ).resolve()
