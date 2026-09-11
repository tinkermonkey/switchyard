"""File logging must be bounded, and must behave exactly like FileHandler otherwise.

The orchestrator's file handlers were plain `logging.FileHandler`, which never
rotates: `orchestrator_data/logs/` reached 9.2GB on the live deployment -- ~97%
of everything it had written to disk, against 31MB of actual state.

Swapping in RotatingFileHandler is a one-line change with two ways to go
wrong, and both were made and caught while writing it. They are pinned here
because neither shows up as a rotation bug -- they show up somewhere else
entirely, weeks later.
"""

import logging
from pathlib import Path

import pytest

from monitoring.log_rotation import (  # noqa: E402
    CHECKOUT_LOG_BACKUP_COUNT,
    CHECKOUT_LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    _positive_int,
    checkout_log_handler,
    rotating_file_handler,
)


@pytest.fixture
def closed_handlers():
    handlers = []
    yield handlers
    for handler in handlers:
        handler.close()


class TestFileHandlerSemanticsArePreserved:
    """The two regressions, both real, both caught by existing tests."""

    def test_the_file_is_opened_eagerly_not_on_first_emit(self, tmp_path, closed_handlers):
        """delay=True would defer the open -- and pipeline/repair_cycle_runner.py
        wraps construction in a try/except to fall back to stdout-only logging
        when the epic worktree it writes into was never mounted. Deferring the
        open moves that OSError out of the try and into the first log call,
        where nothing handles it. The guard exists because of a real incident;
        disarming it silently is worse than not rotating.
        """
        path = tmp_path / 'eager.log'

        handler = rotating_file_handler(path)
        closed_handlers.append(handler)

        assert path.exists(), "the handler must open (and create) its file at construction"

    def test_a_missing_parent_directory_raises_rather_than_being_created(
        self, tmp_path, closed_handlers
    ):
        """Same guard, other half. An mkdir(parents=True) here makes the open
        succeed against a worktree that is not mounted -- so the runner never
        learns, and a stray directory tree appears inside a project checkout.
        """
        path = tmp_path / 'never-mounted' / 'worktree' / '.repair_cycle.log'

        with pytest.raises(OSError):
            closed_handlers.append(rotating_file_handler(path))

        assert not path.parent.exists()

    def test_level_and_formatter_are_applied_when_given(self, tmp_path, closed_handlers):
        formatter = logging.Formatter('%(message)s')

        handler = rotating_file_handler(
            tmp_path / 'configured.log', level=logging.WARNING, formatter=formatter
        )
        closed_handlers.append(handler)

        assert handler.level == logging.WARNING
        assert handler.formatter is formatter


class TestBounding:

    def test_writing_past_the_cap_rotates_instead_of_growing(self, tmp_path, closed_handlers):
        path = tmp_path / 'bounded.log'
        handler = rotating_file_handler(path)
        closed_handlers.append(handler)
        handler.maxBytes = 200
        handler.backupCount = 2
        handler.setFormatter(logging.Formatter('%(message)s'))

        for i in range(200):
            handler.emit(logging.LogRecord(
                'test', logging.INFO, __file__, i, 'x' * 50, None, None
            ))

        assert path.stat().st_size <= 200 + 64, "the live file stays under the cap"
        rotated = sorted(p.name for p in tmp_path.glob('bounded.log.*'))
        assert rotated == ['bounded.log.1', 'bounded.log.2'], \
            "exactly backupCount rotations are kept, oldest dropped"

    def test_the_defaults_bound_a_log_to_about_a_gigabyte(self):
        """A ceiling small enough that grepping the file is still possible --
        which the 8.6GB file it replaces was not."""
        ceiling = (LOG_BACKUP_COUNT + 1) * LOG_MAX_BYTES
        assert ceiling <= 2 * 1024 ** 3


class TestOverrides:

    def test_a_malformed_override_falls_back_to_the_default(self):
        """This is read during startup, before there is anywhere useful to
        report a bad value to. Refusing to log at all is the wrong answer."""
        import os
        os.environ['SWITCHYARD_TEST_BOGUS_INT'] = 'not-a-number'
        try:
            assert _positive_int('SWITCHYARD_TEST_BOGUS_INT', 42) == 42
        finally:
            del os.environ['SWITCHYARD_TEST_BOGUS_INT']

    def test_a_nonpositive_override_falls_back_to_the_default(self):
        """maxBytes=0 means "never rotate" to RotatingFileHandler -- i.e. it
        would silently reinstate exactly the unbounded behaviour this replaces."""
        import os
        os.environ['SWITCHYARD_TEST_BOGUS_INT'] = '0'
        try:
            assert _positive_int('SWITCHYARD_TEST_BOGUS_INT', 42) == 42
        finally:
            del os.environ['SWITCHYARD_TEST_BOGUS_INT']

    def test_a_valid_override_is_honoured(self):
        import os
        os.environ['SWITCHYARD_TEST_BOGUS_INT'] = '7'
        try:
            assert _positive_int('SWITCHYARD_TEST_BOGUS_INT', 42) == 7
        finally:
            del os.environ['SWITCHYARD_TEST_BOGUS_INT']


class TestNoPlainFileHandlersRemain:

    def test_no_module_still_constructs_an_unbounded_file_handler(self):
        """The whole point. A new logging.FileHandler anywhere in the tree
        reintroduces the 9.2GB, and it will not be noticed for months."""
        import re

        root = Path(__file__).parent.parent.parent
        offenders = []
        for source in root.rglob('*.py'):
            relative = source.relative_to(root)
            parts = relative.parts
            if parts[0] in ('tests', '.claude', 'node_modules', 'venv', '.venv'):
                continue
            text = source.read_text(errors='ignore')
            if re.search(r'(?<!Rotating)\blogging\.FileHandler\s*\(', text):
                offenders.append(str(relative))

        assert offenders == [], (
            f"these modules construct an unbounded logging.FileHandler: {offenders}. "
            f"Use monitoring.log_rotation.rotating_file_handler() instead."
        )


class TestPerCheckoutCap:
    """`.repair_cycle.log` exists once per managed checkout, not once per process."""

    def test_the_checkout_cap_is_far_below_the_orchestrator_one(self):
        """There are 17 checkouts on the live deployment, so the
        orchestrator-wide 256MB x 4 would put a ~17GB ceiling on a single
        filename -- against 309MB actually on disk. The whole-fleet worst case
        at these caps is ~800MB."""
        fleet = 20
        orchestrator_ceiling = (LOG_BACKUP_COUNT + 1) * LOG_MAX_BYTES
        checkout_ceiling = (CHECKOUT_LOG_BACKUP_COUNT + 1) * CHECKOUT_LOG_MAX_BYTES

        assert checkout_ceiling < orchestrator_ceiling
        assert checkout_ceiling * fleet < orchestrator_ceiling

    def test_the_checkout_handler_applies_those_caps(self, tmp_path, closed_handlers):
        handler = checkout_log_handler(tmp_path / '.repair_cycle.log')
        closed_handlers.append(handler)

        assert handler.maxBytes == CHECKOUT_LOG_MAX_BYTES
        assert handler.backupCount == CHECKOUT_LOG_BACKUP_COUNT

    def test_it_keeps_the_eager_open_the_runner_depends_on(self, tmp_path, closed_handlers):
        """Same guard as the orchestrator handler: the repair-cycle runner
        detects an unmounted epic worktree by this raising."""
        missing = tmp_path / 'never-mounted' / '.repair_cycle.log'

        with pytest.raises(OSError):
            closed_handlers.append(checkout_log_handler(missing))

        assert not missing.parent.exists()
