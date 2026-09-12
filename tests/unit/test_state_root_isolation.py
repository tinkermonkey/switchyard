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
        stop referring to what their consumers actually use. 26 non-test call
        sites do a function-level `from config.state_manager import
        state_manager` -- 20 of them in services/project_monitor.py alone --
        and would pick up the new one while module-level importers kept the
        old.

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
        scan was passing. Three were writers, and TWO of them mkdir'd in a
        constructor (pr_review_checkpoint.py, repair_cycle_checkpoint.py).

        So this matches on CO-OCCURRENCE rather than on syntax. It will
        overmatch eventually; that is the intended direction. Add to EXEMPT
        with a reason rather than narrowing the pattern.

        WHAT IT STILL MISSES, measured rather than guessed: it scans ONE LINE
        AT A TIME, so splitting the derivation from the `"state"` literal
        defeats it -- `_root = Path(__file__).parent.parent` on one line and
        `_root / "state"` on the next passes clean, as does any `<var> /
        "state"`. Five such sites already exist in scripts/ and mcp/ and are
        not flagged. This is a tripwire for the shapes that caused #181, not a
        proof that they cannot recur; closing it properly wants an AST walk
        from each mkdir/open back to its root, which is tracked separately.
        """
        import re

        root = Path(__file__).parent.parent.parent

        # path -> why it legitimately names one of these
        #
        # NO DEAD ENTRIES: the assertion at the end of this test fails if a
        # listed file has stopped matching. Two were removed under that rule
        # when it was added.
        #
        # `config/state_manager.py`, exempted as "defines the resolver", was
        # the one that mattered: #202 moved orchestrator_state_root(), and with
        # it the `Path(__file__).parent.parent / "state"` fallback, out to
        # config/paths.py, so state_manager.py had stopped matching either
        # pattern -- leaving the single file that CAUSED #181 blanket-exempted
        # from the tripwire that exists to catch #181, for a reason that no
        # longer applied.
        #
        # `mcp/server.py` was exempted as "separate service,
        # APP_ROOT-configurable" and documented as already dead, on the
        # argument that it would earn its place once the patterns grew strong
        # enough to see `<var> / "state"`. An exemption that is only justified
        # by a hypothetical future match is indistinguishable from one whose
        # reason has expired, which is the failure above; re-add it on the day
        # the patterns change.
        EXEMPT = {
            # Its three matches are `relative_path=` values in the rule table
            # -- relative SEGMENTS hung off a root that is itself resolved from
            # ORCHESTRATOR_ROOT/WORKSPACE_ROOT (see resolve_roots there). It
            # does open them; the resolution is what makes that safe. Exempting
            # the whole file is broader than that reason justifies -- an
            # absolute or __file__-derived path added to this module later
            # would be invisible.
            'services/data_retention.py': 'rule-table relative segments',
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
        exemptions_used = set()
        for source in sorted(root.rglob('*.py')):
            relative = source.relative_to(root)
            if relative.parts[0] in ('tests', '.claude', 'node_modules', 'venv', '.venv'):
                continue
            for number, line in enumerate(source.read_text(errors='ignore').splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if derived_from_file.search(line) or literal_state_root.search(line):
                    if str(relative) in EXEMPT:
                        exemptions_used.add(str(relative))
                    else:
                        offenders.append(f"{relative}:{number}: {stripped[:90]}")

        assert offenders == [], (
            "these lines build a state path from the code's own location or "
            "from a literal, either of which ignores ORCHESTRATOR_ROOT and "
            "writes into the deployment (#181):\n  " + "\n  ".join(offenders) +
            "\nUse config.state_manager.orchestrator_state_root()."
        )

        # An exemption whose file has stopped matching is a permanent blind
        # spot with no remaining justification, and the file it covers is
        # usually the one most worth watching -- config/state_manager.py sat
        # here, exempted, for exactly that reason after #202 moved the resolver
        # out from under it.
        assert set(EXEMPT) == exemptions_used, (
            "these EXEMPT entries no longer match anything, so they only "
            "hide whatever is added to those files next -- delete them: "
            f"{sorted(set(EXEMPT) - exemptions_used)}"
        )


class TestTheSuiteCannotReachTheDeploymentsState:

    def test_the_active_root_would_still_pass_the_import_time_guard(self):
        """The st_dev/st_ino identity check itself now lives in
        tests/conftest.py::_refuse_a_root_that_can_reach_the_deployment, where
        it runs at conftest import and RAISES (#202). Here it only ran partway
        through the session, so everything alphabetically before it had already
        written wherever the bad root pointed -- detection, not prevention, and
        22 tests including all 11 of this file's were observed green with
        ORCHESTRATOR_ROOT pointed at the checkout.

        What is left here is the end-state assertion: whatever the session is
        actually running with still satisfies the guard. That is not a
        tautology -- a test that reassigns os.environ['ORCHESTRATOR_ROOT']
        session-wide would break it, and conftest's guard would never see it.
        """
        from tests.conftest import _refuse_a_root_that_can_reach_the_deployment

        active = os.environ.get('ORCHESTRATOR_ROOT')
        assert active, "conftest must have redirected ORCHESTRATOR_ROOT"

        _refuse_a_root_that_can_reach_the_deployment(active, 'ORCHESTRATOR_ROOT')


class TestTheImportTimeGuardOnTheRoot:
    """tests/conftest.py::_refuse_a_root_that_can_reach_the_deployment.

    It used to be that `if os.environ.get('ORCHESTRATOR_ROOT'): return` --
    any preset value accepted as given, which is how the whole suite could be
    pointed at the live checkout and stay green (#202).
    """

    @staticmethod
    def _guard():
        from tests.conftest import _refuse_a_root_that_can_reach_the_deployment
        return _refuse_a_root_that_can_reach_the_deployment

    @staticmethod
    def _skip_without_a_deployment():
        if not Path('/app').is_dir():
            pytest.skip("no /app on this host; nothing to collide with")

    def test_an_absolute_scratch_root_is_accepted(self, tmp_path):
        assert self._guard()(str(tmp_path), 'ORCHESTRATOR_ROOT') == str(tmp_path)

    def test_a_relative_root_is_refused(self):
        with pytest.raises(RuntimeError, match='relative'):
            self._guard()('tmp/rv202', 'ORCHESTRATOR_ROOT')

    def test_the_deployment_directory_itself_is_refused(self):
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app', 'ORCHESTRATOR_ROOT')

    def test_an_alias_of_the_deployment_directory_is_refused(self):
        """Spelling is not identity. `/app/../app` normalises to the same inode
        and is exactly the shape `!= '/app'` used to wave through."""
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app/../app', 'ORCHESTRATOR_ROOT')

    def test_a_path_under_the_deployment_is_refused(self):
        """Stricter than the identity check this replaces, deliberately: the
        caller mkdir(parents=True)s an accepted root, so `/app/scratch` does
        not merely read production, it creates a directory inside it."""
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app/scratch-that-does-not-exist', 'ORCHESTRATOR_ROOT')

    def test_the_live_state_tree_is_refused(self):
        """The worst case, and the one #181 actually hit."""
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app/state', 'ORCHESTRATOR_ROOT')

    def test_the_message_names_the_variable_it_was_given(self):
        """Both callers share this function; an error naming the wrong
        environment variable sends the operator to the wrong place."""
        with pytest.raises(RuntimeError, match='SWITCHYARD_TEST_STATE_ROOT'):
            self._guard()('still-relative', 'SWITCHYARD_TEST_STATE_ROOT')

    def test_a_preset_root_goes_through_the_guard(self, monkeypatch):
        """The redirect honours a preset ORCHESTRATOR_ROOT -- the documented
        `docker exec -e ORCHESTRATOR_ROOT=/tmp/...` invocation depends on it --
        but no longer unvalidated."""
        self._skip_without_a_deployment()
        from tests.conftest import _redirect_orchestrator_root_to_scratch

        monkeypatch.setenv('ORCHESTRATOR_ROOT', '/app')

        with pytest.raises(RuntimeError, match='deployment'):
            _redirect_orchestrator_root_to_scratch()

    @pytest.mark.parametrize('preset', ['', '   '], ids=['empty', 'whitespace'])
    def test_a_blank_preset_is_redirected_to_scratch_not_read_as_unset(
        self, monkeypatch, preset
    ):
        """orchestrator_state_root() reads blank as unset and falls back to the
        checkout -- which is the deployment. conftest must NOT agree with it
        here: a blank preset has to become a scratch directory.

        The whitespace case is the one that was broken: `if
        os.environ.get('ORCHESTRATOR_ROOT'): return` saw `'   '` as truthy and
        returned, leaving it set, and every module then resolved the checkout.
        `''` was already falsy and fell through; it is parametrized alongside
        so the two cannot diverge again.
        """
        from tests.conftest import _redirect_orchestrator_root_to_scratch

        monkeypatch.setenv('ORCHESTRATOR_ROOT', preset)
        monkeypatch.delenv('SWITCHYARD_TEST_STATE_ROOT', raising=False)

        _redirect_orchestrator_root_to_scratch()

        chosen = os.environ['ORCHESTRATOR_ROOT']
        assert chosen.strip(), f"a blank preset ({preset!r}) was left in place"
        assert Path(chosen).is_dir()
        self._guard()(chosen, 'ORCHESTRATOR_ROOT')

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
