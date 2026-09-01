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
    lock_manager = Mock()
    lock_manager.clear_retained_reason = Mock(return_value=True)
    wd = PipelineWatchdog(
        es_client=Mock(),
        pipeline_run_manager=pipeline_run_manager,
        lock_manager=lock_manager,
        project_monitor=Mock(),
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
    """
    _redispatch_same_issue is patched to a plain success/failure stub in most
    of these -- its own GraphQL/config-lookup internals are covered
    separately in TestRedispatchSameIssue. What matters here is _cleanup_
    zombie_run's own decision logic: does it retain the lock throughout, and
    does it call clear_retained_reason/_redispatch_same_issue at the right
    times.
    """

    def test_self_heals_within_budget_lock_never_released(self, watchdog):
        """Within budget: end_pipeline_run always retains the lock (never
        retain_lock=False — see incident e42ca133, where releasing it let a
        different issue steal the lock before the zombie's own issue could be
        retried). The self-heal then clears the durable mark and redispatches
        the SAME issue directly, so the lock stays held by that issue for the
        whole window."""
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=True) as redispatch:
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
            assert call.kwargs["retain_lock"] is True
        assert watchdog.lock_manager.clear_retained_reason.call_count == ZOMBIE_AUTO_RETRY_LIMIT
        assert redispatch.call_count == ZOMBIE_AUTO_RETRY_LIMIT
        notify.assert_not_called()

    def test_retains_lock_and_alerts_once_budget_exhausted(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=True) as redispatch:
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
        # The exhausted (final) attempt must NOT clear the durable mark or
        # redispatch — only the first ZOMBIE_AUTO_RETRY_LIMIT self-heal
        # attempts do that.
        assert watchdog.lock_manager.clear_retained_reason.call_count == ZOMBIE_AUTO_RETRY_LIMIT
        assert redispatch.call_count == ZOMBIE_AUTO_RETRY_LIMIT
        notify.assert_called_once()
        notify_args = notify.call_args.args
        assert notify_args[0] == "proj"
        assert notify_args[2] == 159
        assert notify_args[4] == ZOMBIE_AUTO_RETRY_LIMIT + 1

    def test_different_issues_have_independent_budgets(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=True) as redispatch:
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
        assert redispatch.call_count == ZOMBIE_AUTO_RETRY_LIMIT * 2

    def test_retains_lock_and_alerts_when_clear_fails(self, watchdog):
        """If clear_retained_reason() itself fails within budget, the lock is
        already durably retained (never released) and a human is notified —
        same as the exhausted-budget outcome. Redispatch must not even be
        attempted, since the retained-lock dispatch gate would refuse it."""
        watchdog.lock_manager.clear_retained_reason = Mock(return_value=False)
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue") as redispatch:
            watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )

        redispatch.assert_not_called()
        notify.assert_called_once()

    def test_restores_durable_retention_when_redispatch_fails_after_clear(self, watchdog):
        """If clear_retained_reason() succeeds but the redispatch itself
        fails, the lock must not be left un-retained with nothing dispatched
        (any other issue could then acquire it) — the durable mark must be
        restored via mark_lock_failed before notifying."""
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=False):
            watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )

        watchdog.lock_manager.clear_retained_reason.assert_called_once()
        watchdog.lock_manager.mark_lock_failed.assert_called_once()
        assert watchdog.lock_manager.mark_lock_failed.call_args.args[:3] == (
            "proj", "SDLC Execution", 159
        )
        notify.assert_called_once()


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
        # workspace -- normal transient-stall self-heal still applies (lock
        # retained throughout, durable mark cleared, same issue redispatched).
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=True) as redispatch, \
             patch.object(watchdog, "_is_workspace_git_broken", return_value=False):
            watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T10:07:24Z",
            )

        call = watchdog.pipeline_run_manager.end_pipeline_run.call_args
        assert call.kwargs["retain_lock"] is True
        watchdog.lock_manager.clear_retained_reason.assert_called_once_with(
            "proj", "SDLC Execution", 42
        )
        redispatch.assert_called_once_with("proj", "SDLC Execution", 42)
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


class TestRedispatchSameIssue:
    """
    PipelineWatchdog._redispatch_same_issue — the self-heal redispatch helper
    both _cleanup_zombie_run and _actively_resume_run rely on. Exercises its
    own GraphQL/config-lookup plumbing directly (the tests above patch this
    method out entirely, since they only care about the caller's decision
    logic).
    """

    def _fake_project_config(self):
        cfg = Mock()
        cfg.github = {"org": "the-org", "repo": "the-repo"}
        return cfg

    def _graphql_response(self, state="OPEN", column="Code Review"):
        nodes = [{"fieldValueByName": {"name": column}}] if column else []
        return (True, {
            "repository": {
                "issue": {
                    "state": state,
                    "projectItems": {"nodes": nodes},
                }
            }
        })

    def test_redispatches_with_current_column_and_repo(self, watchdog):
        github_client = Mock()
        github_client.graphql.return_value = self._graphql_response(column="Code Review")

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()), \
             patch("services.github_api_client.get_github_client", return_value=github_client):
            result = watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        assert result is True
        watchdog.project_monitor.trigger_agent_for_status.assert_called_once_with(
            project_name="proj",
            board_name="SDLC Execution",
            issue_number=159,
            status="Code Review",
            repository="the-repo",
            lock_already_acquired=True,
        )

    def test_returns_false_when_no_project_monitor(self, watchdog):
        watchdog.project_monitor = None
        with patch("services.project_monitor.get_project_monitor", return_value=None):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False

    def test_returns_false_when_project_config_missing(self, watchdog):
        with patch("config.manager.config_manager.get_project_config", return_value=None):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_returns_false_when_graphql_fails(self, watchdog):
        github_client = Mock()
        github_client.graphql.return_value = (False, "boom")

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()), \
             patch("services.github_api_client.get_github_client", return_value=github_client):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_returns_false_when_issue_closed(self, watchdog):
        github_client = Mock()
        github_client.graphql.return_value = self._graphql_response(state="CLOSED")

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()), \
             patch("services.github_api_client.get_github_client", return_value=github_client):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_returns_false_when_no_current_column(self, watchdog):
        github_client = Mock()
        github_client.graphql.return_value = self._graphql_response(column=None)

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()), \
             patch("services.github_api_client.get_github_client", return_value=github_client):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_returns_false_on_unexpected_exception(self, watchdog):
        with patch("config.manager.config_manager.get_project_config", side_effect=RuntimeError("boom")):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()
