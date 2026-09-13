"""
No test file may leave a mock in sys.modules for a first-party module (#133).

The concrete instance this was written for: three test files did

    if 'services.dev_container_state' not in sys.modules:
        sys.modules['services.dev_container_state'] = MagicMock()

at module scope, to get around both that module and
services.work_execution_state constructing their singleton at import time under
`Path(os.environ.get('ORCHESTRATOR_ROOT', '/app')) / "state" / ...`, which
cannot be created on a machine without a writable /app.

Nothing removed those entries afterwards, so they applied to the whole pytest
session. claude/docker_runner.py's _get_image_for_agent() does
`from services.dev_container_state import dev_container_state` at call time and
got the MagicMock, `is_verified()`/`get_image_name()` returned MagicMocks, and
_build_docker_command() appended one as the container image -- which is what
made tests/unit/test_docker_runner_worktree_mount.py's
`' '.join(cmd)` raise "TypeError: sequence item 29: expected str instance,
MagicMock found", but only when those files shared a session in that order.
#133 attributed the leak to the `patch('claude.docker_runner.subprocess')`
context managers in the same file; those are correctly scoped and were not
involved.

tests/conftest.py now sets ORCHESTRATOR_ROOT to a scratch directory when /app is
absent, so the real modules import everywhere and the workaround is unnecessary.
This test is the tripwire for the next file that reaches for it anyway --
sampled both at collection finish (the module-scope shape) and at session finish
(the run-time shape test_docker_runner_validation.py used).
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import FIRST_PARTY_PACKAGES, leaked_module_mocks

# Module scope on purpose, and a plain import: TestConftestRestoresBothHalves
# OfAReimport at the bottom of this file needs services.work_execution_state to
# already be in sys.modules when its FIRST test is set up, because conftest's
# _restore_process_globals only puts back names it saw at that test's setup.
# Importing it here is what makes that pair independent of what else the
# selection happens to contain. Safe under #181: conftest points
# ORCHESTRATOR_ROOT at a writable scratch root before any test module is
# imported, which is the whole point of the file this line lives in.
import services.work_execution_state  # noqa: E402


def test_no_first_party_module_was_replaced_by_a_mock_during_collection():
    assert leaked_module_mocks == [], (
        "These modules were replaced by mocks in sys.modules while pytest was "
        f"importing test files, and nothing put them back: {leaked_module_mocks}. "
        "Every test that imports one of them for the rest of this session gets "
        "the mock, whichever file it belongs to -- which is how the same suite "
        "produces different results depending on how it is chunked. Use a "
        "fixture-scoped patch, or fix what makes the real import fail."
    )


def test_the_packages_it_guards_include_the_ones_that_were_actually_leaked():
    """A regression on the guard's own scope: both modules #133 involved live
    under `services`, and the two importers that consumed them under `claude`
    and `agents`."""
    for package in ('services', 'claude', 'agents'):
        assert package in FIRST_PARTY_PACKAGES


class TestTheDetectorItself:
    """The detector runs once per session against real state, so drive it
    directly for the cases a normal run should never produce."""

    def test_a_mocked_first_party_module_is_reported(self):
        import sys
        from unittest.mock import MagicMock, patch

        from tests.conftest import _first_party_modules_replaced_by_mocks

        with patch.dict(sys.modules, {'services.__leak_probe__': MagicMock()}):
            assert 'services.__leak_probe__' in _first_party_modules_replaced_by_mocks()

    def test_a_mocked_third_party_module_is_not_reported(self):
        import sys
        from unittest.mock import MagicMock, patch

        from tests.conftest import _first_party_modules_replaced_by_mocks

        with patch.dict(sys.modules, {'some_optional_dep': MagicMock()}):
            assert 'some_optional_dep' not in _first_party_modules_replaced_by_mocks()

    def test_the_real_modules_the_workaround_targeted_import_for_real(self):
        """Both of these used to be un-importable outside the container, which
        is the whole reason the workaround existed."""
        import services.dev_container_state
        import services.work_execution_state

        assert services.dev_container_state.dev_container_state.state_dir.is_dir()
        assert services.work_execution_state.work_execution_tracker.state_dir.is_dir()


class TestALeakInsertedWhileTestsRanIsCaughtToo:
    """
    Found in review: the collection-time snapshot covers two of the three files
    #133 was about and not the third. tests/unit/test_docker_runner_validation.py
    did the assignment inside a helper method --

        def _get_non_retryable_class(self):
            if 'services.dev_container_state' not in sys.modules:
                sys.modules['services.dev_container_state'] = MagicMock()

    -- which runs long after pytest_collection_finish has taken its sample, so
    the guard written to catch the next file reaching for the workaround would
    have stayed green through exactly the shape that file used. A fixture or a
    setUp is the natural place for the next one now that the module-scope form
    is visibly discouraged.
    """

    @staticmethod
    def _session():
        return SimpleNamespace(
            config=SimpleNamespace(
                pluginmanager=SimpleNamespace(get_plugin=lambda name: None)
            ),
            exitstatus=pytest.ExitCode.OK,
        )

    def test_a_module_mocked_after_collection_fails_the_session(self):
        from unittest.mock import patch

        from tests import conftest

        session = self._session()
        with patch.object(conftest, 'leaked_module_mocks', []), \
                patch.object(
                    conftest, '_first_party_modules_replaced_by_mocks',
                    return_value=['services.dev_container_state'],
                ):
            conftest.pytest_sessionfinish(session, session.exitstatus)

        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_a_leak_already_reported_at_collection_is_not_counted_twice(self):
        """That one already fails test_no_first_party_module_was_replaced_by_a_mock
        _during_collection by name, which is the better report."""
        from unittest.mock import patch

        from tests import conftest

        session = self._session()
        with patch.object(conftest, 'leaked_module_mocks', ['services.dev_container_state']), \
                patch.object(
                    conftest, '_first_party_modules_replaced_by_mocks',
                    return_value=['services.dev_container_state'],
                ):
            conftest.pytest_sessionfinish(session, session.exitstatus)

        assert session.exitstatus == pytest.ExitCode.OK

    def test_a_clean_session_is_left_alone(self):
        from unittest.mock import patch

        from tests import conftest

        session = self._session()
        with patch.object(conftest, 'leaked_module_mocks', []), \
                patch.object(
                    conftest, '_first_party_modules_replaced_by_mocks', return_value=[],
                ):
            conftest.pytest_sessionfinish(session, session.exitstatus)

        assert session.exitstatus == pytest.ExitCode.OK


class TestConftestRestoresBothHalvesOfAReimport:
    """sys.modules is only HALF of what an import binds, and the restore fixture
    used to put back only that half (#221).

    `import services.work_execution_state as wes` does not read sys.modules for
    the name it binds: it imports the module, then binds
    `getattr(services, 'work_execution_state')`, consulting sys.modules only if
    that attribute is missing. A test that pops the module and re-imports it
    sets that attribute on the `services` package object, and restoring
    sys.modules alone leaves the two halves disagreeing -- `import_module()` and
    `from services.x import y` see the restored module while `import services.x
    as y` still sees the replacement, along with its singleton bound to a
    tmp_path pytest has deleted.

    That is not hypothetical: it is why
    tests/unit/scripts/test_dry_run_state_sweep.py's
    TestRuntimeSingletonBinding pair failed in any selection that ran a popping
    file first. Measured on the pre-#221 tree, fresh root:
    `pytest tests/unit/services/test_stale_execution_history.py
    tests/unit/scripts/test_dry_run_state_sweep.py` -> 2 failed, 95 passed; with
    the sys.modules half restored but not the package attribute, still 1 failed.

    The three files that did the pop no longer do (#221 removed the workaround
    at its source), so this pair is what keeps the fixture's other half honest
    for the next one.
    """

    def test_step_one_a_pop_and_reimport_replaces_both_halves(self, tmp_path, monkeypatch):
        """Half one of a pair -- the test below is the assertion that matters.
        This one deliberately commits the anti-pattern and pins what it does to
        BOTH halves, so the next test's subject is established rather than
        assumed."""
        import importlib

        import services

        before = sys.modules['services.work_execution_state']
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        sys.modules.pop('services.work_execution_state')
        replacement = importlib.import_module('services.work_execution_state')

        assert replacement is not before
        assert sys.modules['services.work_execution_state'] is replacement
        assert getattr(services, 'work_execution_state') is replacement, (
            "the import system sets the submodule attribute on the parent "
            "package; if it stopped doing that this pair no longer tests "
            "anything"
        )
        assert Path(replacement.work_execution_tracker.state_dir) == (
            tmp_path / 'state' / 'execution_history'
        )

    def test_step_two_both_halves_came_back(self):
        """Depends on running after the test above, and cannot be folded into
        it: the restore happens in that test's teardown, which is not observable
        from inside it. Same shape, and the same reason, as
        tests/unit/scripts/test_dry_run_state_sweep.py's step_one/step_two."""
        import services
        import services.work_execution_state as via_import_statement

        restored = sys.modules['services.work_execution_state']

        assert getattr(services, 'work_execution_state') is restored
        assert via_import_statement is restored
        root = Path(os.environ['ORCHESTRATOR_ROOT'])
        assert Path(via_import_statement.work_execution_tracker.state_dir) == (
            root / 'state' / 'execution_history'
        )
