"""
Regression tests for #148: the review-cycle thread's own teardown decisions.

tests/unit/services/test_review_cycle_lock_timeout.py covers the executor half
(ReviewCycleExecutor._release_run_for_lock_contention: release the run, don't
mark_failed, keep the cycle state) and stops at the thread boundary. These cover
what project_monitor's review-cycle thread owes on the way out once that
exception reaches it.

Two decisions, both extracted to module level for the same reason
classify_repair_cycle_outcome() and _end_pr_review_pipeline_run_on_failure()
were — the live code is a closure inside a daemon thread that is impractical to
drive from a test:

  * classify_review_cycle_thread_exception(): only a genuine crash may reach
    mark_failed(), which durably retains the BOARD's pipeline lock pending
    scripts/release_lock.py.
  * review_cycle_thread_teardown(): 'contention' is neither 'crash' nor 'keep'.
    The executor has already ended the run with retain_lock=False, but the queue
    entry is still 'active' — and nothing else moves an 'active' entry back to
    'waiting', while get_next_waiting_issue() filters strictly on 'waiting'. Fold
    contention into 'keep' (or drop the reset) and the issue is silently excluded
    from every future dispatch on that board with no failure recorded, no comment
    posted, and no further dispatch for the escalation valve to fire on.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.project_monitor import (
    classify_review_cycle_thread_exception,
    review_cycle_thread_teardown,
)
from services.cancellation import CancellationError
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


class TestClassifyReviewCycleThreadException:
    def test_a_checkout_lock_timeout_is_contention_not_a_crash(self):
        exc = ProjectCheckoutLockTimeoutError(
            "Could not acquire 'project_checkout' lock within 10900.0s"
        )
        assert classify_review_cycle_thread_exception(exc) == 'lock_contention'

    def test_a_dev_container_build_lock_timeout_is_contention_too(self):
        exc = DevContainerBuildLockTimeoutError("build slot busy")
        assert classify_review_cycle_thread_exception(exc) == 'lock_contention'

    def test_a_wrapped_lock_timeout_is_still_contention(self):
        """Agents re-wrap whatever run_claude_code() raises into a plain
        Exception(...) from exc — the __cause__ walk is what keeps the type
        visible at this boundary."""
        cause = ProjectCheckoutLockTimeoutError("held by #99")
        try:
            try:
                raise cause
            except ProjectCheckoutLockTimeoutError as inner:
                raise Exception("Agent execution failed") from inner
        except Exception as wrapped:
            assert classify_review_cycle_thread_exception(wrapped) == 'lock_contention'

    def test_a_cancellation_is_neither_a_crash_nor_contention(self):
        assert classify_review_cycle_thread_exception(
            CancellationError("stopped by operator")
        ) == 'cancelled'

    def test_an_ordinary_exception_is_a_crash(self):
        assert classify_review_cycle_thread_exception(
            RuntimeError("review cycle blew up")
        ) == 'crash'


class TestReviewCycleThreadTeardown:
    def test_contention_is_its_own_verdict_not_a_crash(self):
        assert review_cycle_thread_teardown(
            is_exit_column=False, exception_occurred=False, lock_contention_occurred=True
        ) == 'contention'

    def test_a_crash_still_wins(self):
        assert review_cycle_thread_teardown(
            is_exit_column=False, exception_occurred=True, lock_contention_occurred=False
        ) == 'crash'

    def test_an_exit_column_releases_and_dispatches_regardless(self):
        assert review_cycle_thread_teardown(
            is_exit_column=True, exception_occurred=True, lock_contention_occurred=True
        ) == 'exit'

    def test_a_clean_intermediate_run_keeps_the_lock_for_the_next_stage(self):
        assert review_cycle_thread_teardown(
            is_exit_column=False, exception_occurred=False, lock_contention_occurred=False
        ) == 'keep'

    def test_contention_is_not_keep(self):
        """The regression that leaves the queue entry stuck at 'active' forever:
        'keep' does no reset, and nothing else ever does one."""
        assert review_cycle_thread_teardown(
            is_exit_column=False, exception_occurred=False, lock_contention_occurred=True
        ) != 'keep'

    @pytest.mark.parametrize("exception_occurred", [True, False])
    @pytest.mark.parametrize("lock_contention_occurred", [True, False])
    @pytest.mark.parametrize("is_exit_column", [True, False])
    def test_every_combination_yields_exactly_one_known_verdict(
        self, is_exit_column, exception_occurred, lock_contention_occurred
    ):
        assert review_cycle_thread_teardown(
            is_exit_column=is_exit_column,
            exception_occurred=exception_occurred,
            lock_contention_occurred=lock_contention_occurred,
        ) in {'exit', 'crash', 'contention', 'keep'}

    # --- card_move_failed (pipeline run 4cf816cf) ---
    #
    # The regression these pin: a review that APPROVED the work, followed by a
    # card move that failed every retry, raised nothing — so exception_occurred
    # was False and this returned 'keep'. 'keep' holds the board's lock without
    # marking the run failed, which left the run 'active' with nothing running.
    # Two hours later a restart's lock recovery found an ordinary-looking lock
    # holder still sitting in 'Code Review' and re-triggered it.

    def test_a_failed_card_move_is_a_crash_not_keep(self):
        assert review_cycle_thread_teardown(
            is_exit_column=False,
            exception_occurred=False,
            lock_contention_occurred=False,
            card_move_failed=True,
        ) == 'crash'

    def test_the_same_inputs_without_the_failed_move_still_keep(self):
        """Control: only card_move_failed moves this off 'keep'."""
        assert review_cycle_thread_teardown(
            is_exit_column=False,
            exception_occurred=False,
            lock_contention_occurred=False,
            card_move_failed=False,
        ) == 'keep'

    def test_card_move_failed_defaults_to_false_for_existing_callers(self):
        """The parameter is additive: every pre-existing 3-argument call keeps
        its exact previous verdict."""
        assert review_cycle_thread_teardown(
            is_exit_column=False, exception_occurred=False, lock_contention_occurred=False
        ) == 'keep'

    def test_an_exit_column_still_wins_over_a_failed_card_move(self):
        assert review_cycle_thread_teardown(
            is_exit_column=True,
            exception_occurred=False,
            lock_contention_occurred=False,
            card_move_failed=True,
        ) == 'exit'

    def test_a_failed_card_move_is_not_downgraded_to_contention(self):
        """'contention' skips mark_failed() by design. A failed card move needs
        it — the board really is stuck and a human really does have to look."""
        assert review_cycle_thread_teardown(
            is_exit_column=False,
            exception_occurred=False,
            lock_contention_occurred=True,
            card_move_failed=True,
        ) == 'crash'

    @pytest.mark.parametrize("card_move_failed", [True, False])
    @pytest.mark.parametrize("exception_occurred", [True, False])
    @pytest.mark.parametrize("lock_contention_occurred", [True, False])
    @pytest.mark.parametrize("is_exit_column", [True, False])
    def test_every_four_way_combination_yields_one_known_verdict(
        self, is_exit_column, exception_occurred, lock_contention_occurred, card_move_failed
    ):
        assert review_cycle_thread_teardown(
            is_exit_column=is_exit_column,
            exception_occurred=exception_occurred,
            lock_contention_occurred=lock_contention_occurred,
            card_move_failed=card_move_failed,
        ) in {'exit', 'crash', 'contention', 'keep'}
