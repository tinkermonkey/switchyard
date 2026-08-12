"""
Tests for PipelineWatchdog's zombie-cleanup retry budget and stuck-lock alerting.

Covers the fix for a deadlock where a deterministically-failing stage (e.g. a
GitHub comment post that never landed) would zombie-cleanup, retain its pipeline
lock forever, and require a human to notice and manually release it. The watchdog
should now auto-release and let the issue retry itself for the first
ZOMBIE_AUTO_RETRY_LIMIT cleanups, then retain the lock (and alert) only once that
budget is exhausted.
"""
from unittest.mock import Mock, patch

import pytest

from services.pipeline_watchdog import PipelineWatchdog, ZOMBIE_AUTO_RETRY_LIMIT


class FakeRedis:
    """Minimal Redis stand-in supporting incr/expire for the retry counter."""

    def __init__(self):
        self.counters = {}
        self.expiries = {}

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, ttl):
        self.expiries[key] = ttl


@pytest.fixture
def watchdog():
    pipeline_run_manager = Mock()
    pipeline_run_manager.redis = FakeRedis()
    pipeline_run_manager.end_pipeline_run = Mock(return_value=True)
    wd = PipelineWatchdog(
        es_client=Mock(),
        pipeline_run_manager=pipeline_run_manager,
        lock_manager=Mock(),
    )
    return wd


class TestZombieRetryCount:
    def test_increments_per_issue(self, watchdog):
        assert watchdog._increment_zombie_retry_count("proj", 42) == 1
        assert watchdog._increment_zombie_retry_count("proj", 42) == 2
        assert watchdog._increment_zombie_retry_count("proj", 43) == 1

    def test_fails_safe_to_exceeded_when_redis_unavailable(self, watchdog):
        watchdog.pipeline_run_manager.redis = None
        count = watchdog._increment_zombie_retry_count("proj", 42)
        assert count > ZOMBIE_AUTO_RETRY_LIMIT


class TestCleanupZombieRunRetryBudget:
    def test_auto_releases_lock_within_budget(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck") as notify:
            for _ in range(ZOMBIE_AUTO_RETRY_LIMIT):
                watchdog._cleanup_zombie_run(
                    pipeline_run_id="run-1",
                    project="proj",
                    board="SDLC Execution",
                    issue_number=159,
                    started_at="2026-08-10T10:07:24Z",
                )

        assert watchdog.pipeline_run_manager.end_pipeline_run.call_count == ZOMBIE_AUTO_RETRY_LIMIT
        for call in watchdog.pipeline_run_manager.end_pipeline_run.call_args_list:
            assert call.kwargs["retain_lock"] is False
        notify.assert_not_called()

    def test_retains_lock_and_alerts_once_budget_exhausted(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck") as notify:
            for _ in range(ZOMBIE_AUTO_RETRY_LIMIT + 1):
                watchdog._cleanup_zombie_run(
                    pipeline_run_id="run-1",
                    project="proj",
                    board="SDLC Execution",
                    issue_number=159,
                    started_at="2026-08-10T10:07:24Z",
                )

        last_call = watchdog.pipeline_run_manager.end_pipeline_run.call_args_list[-1]
        assert last_call.kwargs["retain_lock"] is True
        notify.assert_called_once()
        notify_args = notify.call_args.args
        assert notify_args[0] == "proj"
        assert notify_args[2] == 159
        assert notify_args[4] == ZOMBIE_AUTO_RETRY_LIMIT + 1

    def test_different_issues_have_independent_budgets(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck") as notify:
            for issue in (159, 160):
                for _ in range(ZOMBIE_AUTO_RETRY_LIMIT):
                    watchdog._cleanup_zombie_run(
                        pipeline_run_id="run-1",
                        project="proj",
                        board="SDLC Execution",
                        issue_number=issue,
                        started_at="2026-08-10T10:07:24Z",
                    )

        notify.assert_not_called()
        assert watchdog.pipeline_run_manager.end_pipeline_run.call_count == ZOMBIE_AUTO_RETRY_LIMIT * 2


class TestCleanupZombieRunBrokenWorkspace:
    """
    Regression coverage for the 2026-08-11 documentation_robotics incident:
    a shared workspace stuck mid-merge (unresolved conflict) caused issues
    #790-#796 to each sail through with a fresh, independent per-issue retry
    budget (see test_different_issues_have_independent_budgets above — that
    behavior is correct for genuinely independent stalls) and get
    auto-released straight back into the same broken checkout, one after
    another, all day. A workspace-level git check must override the
    per-issue budget so this can't happen again, on the very first attempt.
    """

    def test_retains_lock_on_first_attempt_when_workspace_has_conflicts(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_is_workspace_git_broken", return_value=True):
            watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="documentation_robotics",
                board="SDLC Execution",
                issue_number=793,
                started_at="2026-08-11T15:26:40Z",
            )

        call = watchdog.pipeline_run_manager.end_pipeline_run.call_args
        assert call.kwargs["retain_lock"] is True
        notify.assert_called_once()

    def test_does_not_consume_retry_budget_illusion_for_next_issue(self, watchdog):
        # Even though each issue number gets its own retry counter (by design,
        # for independent stalls), a broken workspace must retain the lock
        # for every issue that hits it -- not just the first.
        with patch.object(watchdog, "_notify_lock_stuck"), \
             patch.object(watchdog, "_is_workspace_git_broken", return_value=True):
            for issue in (790, 791, 792, 793):
                watchdog._cleanup_zombie_run(
                    pipeline_run_id="run-1",
                    project="documentation_robotics",
                    board="SDLC Execution",
                    issue_number=issue,
                    started_at="2026-08-11T15:26:40Z",
                )

        for call in watchdog.pipeline_run_manager.end_pipeline_run.call_args_list:
            assert call.kwargs["retain_lock"] is True

    def test_healthy_workspace_still_uses_normal_retry_budget(self, watchdog):
        # Sanity check: the new check must not change behavior for a clean
        # workspace -- normal transient-stall auto-retry still applies.
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_is_workspace_git_broken", return_value=False):
            watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T10:07:24Z",
            )

        call = watchdog.pipeline_run_manager.end_pipeline_run.call_args
        assert call.kwargs["retain_lock"] is False
        notify.assert_not_called()


class TestIsWorkspaceGitBroken:
    def test_true_when_unmerged_paths_present(self, watchdog):
        completed = Mock(returncode=0, stdout=".mcp.json\n")
        with patch("services.pipeline_watchdog.subprocess.run", return_value=completed) as run:
            assert watchdog._is_workspace_git_broken("documentation_robotics") is True
        run.assert_called_once()
        assert run.call_args.kwargs["cwd"] == "/workspace/documentation_robotics"

    def test_false_when_clean(self, watchdog):
        completed = Mock(returncode=0, stdout="")
        with patch("services.pipeline_watchdog.subprocess.run", return_value=completed):
            assert watchdog._is_workspace_git_broken("proj") is False

    def test_false_when_not_a_git_repo(self, watchdog):
        completed = Mock(returncode=128, stdout="")
        with patch("services.pipeline_watchdog.subprocess.run", return_value=completed):
            assert watchdog._is_workspace_git_broken("proj") is False

    def test_fails_open_on_exception(self, watchdog):
        with patch("services.pipeline_watchdog.subprocess.run", side_effect=OSError("boom")):
            assert watchdog._is_workspace_git_broken("proj") is False
