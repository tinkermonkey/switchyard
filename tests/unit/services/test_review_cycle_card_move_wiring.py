"""
Regression tests for the wiring that makes a failed card move end its run.

Why these are source-inspection tests
-------------------------------------
The code under test is a closure (`run_cycle_in_thread`) inside
ProjectMonitor._start_review_cycle_for_issue(), launched on a daemon thread
after a lock acquisition, a workspace resolution and a full review cycle. It is
not drivable from a unit test. This module follows the same technique, and for
the same reason, as tests/unit/services/test_project_monitor_board_lock_heartbeat.py
(which asserts the heartbeat's position in monitor_projects()'s loop) and
tests/unit/test_main_startup_lock_recovery.py (which asserts main.py's startup
ordering): read the source and assert the invariant that a reviewer would
otherwise have to hold in their head.

They are implementation-coupled by construction, and that is the trade. Without
them the fix is REVERT-PROOF: deleting `moved = ` and the `if not moved:` block
restores the incident exactly, and the entire suite — including every test of
review_cycle_thread_teardown()'s verdict table — still passes. The verdict table
was never the bug. The discarded return value was.

The incident (pipeline run 4cf816cf): a review approved issue #1045, the card
move to "Testing" failed all three attempts, `_move_card_with_retry()` returned
False, the caller discarded it, the thread raised nothing, so the teardown read
'keep' — lock held, run still 'active' with nothing running, failure recorded
only as a log line. Two hours later a restart's lock recovery found an
ordinary-looking lock holder and re-triggered it.
"""

import inspect
import os
import re
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.project_monitor import ProjectMonitor


@pytest.fixture(scope='module')
def cycle_source():
    return inspect.getsource(ProjectMonitor._start_review_cycle_for_issue)


class TestTheCardMoveResultIsActedOn:
    def test_the_call_site_captures_the_return_value(self, cycle_source):
        """A bare `self._move_card_with_retry(...)` is the incident."""
        assert re.search(
            r'\bmoved\s*=\s*self\._move_card_with_retry\(', cycle_source
        ), (
            "_move_card_with_retry()'s return value must be captured — discarding "
            "it is what left the run 'active' with nothing running"
        )

    def test_a_failed_move_sets_the_teardown_flag(self, cycle_source):
        assert re.search(
            r'if\s+not\s+moved\s*:.*?card_move_failed\s*=\s*True',
            cycle_source,
            re.DOTALL,
        ), "a False result must set card_move_failed"

    def test_the_flag_reaches_the_teardown_decision(self, cycle_source):
        assert 'card_move_failed=card_move_failed' in cycle_source, (
            "review_cycle_thread_teardown() must receive the flag, or the "
            "decision it was added to inform never sees it"
        )

    def test_a_failed_move_also_sets_an_error_summary(self, cycle_source):
        """The 'crash' branch builds mark_failed()'s reason from error_summary.
        Leaving it None records the retained lock with no stated cause — which
        is most of what made the original incident expensive to investigate."""
        block = re.search(
            r'if\s+not\s+moved\s*:(.*?)(?=\n\s{16}except|\Z)', cycle_source, re.DOTALL
        )
        assert block, "expected an `if not moved:` block"
        assert 'error_summary' in block.group(1)


class TestTheTeardownDistinguishesTheTwoCrashCauses:
    def test_the_fail_reason_branches_on_card_move_failed(self, cycle_source):
        """An operator reading the retained-lock record has to be able to tell a
        thread that raised from an approved review whose card move failed."""
        assert re.search(
            r'card_move_failed\s+and\s+not\s+exception_occurred', cycle_source
        ), "the fail_reason/crash_cause wording must distinguish the two causes"

    def test_both_branches_still_produce_a_reason(self, cycle_source):
        assert 'fail_reason = ' in cycle_source
        assert 'mark_failed(' in cycle_source
