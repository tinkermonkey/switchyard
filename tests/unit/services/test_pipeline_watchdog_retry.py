"""
Tests for PipelineWatchdog's zombie-cleanup retry budget and stuck-lock alerting.

Covers the fix for a deadlock where a deterministically-failing stage (e.g. a
GitHub comment post that never landed) would zombie-cleanup, retain its pipeline
lock forever, and require a human to notice and manually release it. The watchdog
should now auto-release and let the issue retry itself for the first
ZOMBIE_AUTO_RETRY_LIMIT cleanups, then retain the lock (and alert) only once that
budget is exhausted.
"""
from unittest.mock import ANY, AsyncMock, Mock, patch

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
    project_monitor = Mock()
    # Default to an open issue -- a bare Mock()'s auto-generated attributes
    # are truthy and .upper()-able, which would silently NOT match 'CLOSED'
    # either way; set this explicitly so tests aren't relying on that
    # accidental-pass behavior for _redispatch_same_issue's closed-issue check.
    project_monitor.get_issue_details.return_value = {"state": "OPEN"}
    wd = PipelineWatchdog(
        es_client=Mock(),
        pipeline_run_manager=pipeline_run_manager,
        lock_manager=lock_manager,
        project_monitor=project_monitor,
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
             patch.object(watchdog, "_redispatch_same_issue") as redispatch, \
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
        # A broken workspace must skip self-heal entirely -- not just retain
        # the lock, but never even attempt to clear it or redispatch (that
        # would just repeat the identical failure for the next issue too).
        watchdog.lock_manager.clear_retained_reason.assert_not_called()
        redispatch.assert_not_called()

    def test_does_not_consume_retry_budget_illusion_for_next_issue(self, watchdog):
        # Even though each issue number gets its own retry counter (by design,
        # for independent stalls), a broken workspace must retain the lock
        # for every issue that hits it -- not just the first.
        with patch.object(watchdog, "_notify_lock_stuck"), \
             patch.object(watchdog, "_redispatch_same_issue") as redispatch, \
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
        watchdog.lock_manager.clear_retained_reason.assert_not_called()
        redispatch.assert_not_called()

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
    own column-lookup/stale-state/dispatch plumbing directly (the tests above
    patch this method out entirely, since they only care about the caller's
    decision logic).

    Column resolution goes through ProjectMonitor.get_issue_column_sync(),
    the same board-aware lookup ProjectMonitor itself uses elsewhere (see
    _get_parent_column_on_board's project.number filter) — not a bespoke
    GraphQL query, so an issue on multiple Projects v2 boards at once can't
    resolve the wrong board's column.
    """

    def _fake_project_config(self):
        cfg = Mock()
        cfg.github = {"org": "the-org", "repo": "the-repo"}
        return cfg

    def test_redispatches_with_current_column_and_repo(self, watchdog):
        watchdog.project_monitor.get_issue_column_sync.return_value = "Code Review"
        watchdog.project_monitor.trigger_agent_for_status.return_value = "task-abc"

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker:
            result = watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        assert result is True
        watchdog.project_monitor.get_issue_details.assert_called_once_with(
            "the-repo", 159, "the-org"
        )
        watchdog.project_monitor.get_issue_column_sync.assert_called_once_with(
            "proj", "SDLC Execution", 159
        )
        watchdog.project_monitor.trigger_agent_for_status.assert_called_once_with(
            project_name="proj",
            board_name="SDLC Execution",
            issue_number=159,
            status="Code Review",
            repository="the-repo",
            lock_already_acquired=True,
        )
        # Stale in_progress state must be abandoned BEFORE the dispatch call,
        # with an empty active_task_ids set — this issue's zombie/frozen
        # container is already confirmed dead by the caller, so every
        # in_progress entry for it is unconditionally stale. The reason text
        # itself isn't the point of this test (see TestAbandonStaleEntriesReason).
        mock_tracker.abandon_stale_in_progress_entries.assert_called_once_with(
            project_name="proj", issue_number=159, active_task_ids=set(), reason=ANY
        )

    def test_returns_false_when_trigger_agent_for_status_returns_none(self, watchdog):
        """The core regression test for the bug this fix addresses: a legitimate
        no-op inside trigger_agent_for_status (retained-lock gate, duplicate
        task, queue-priority wait, etc.) must be treated as a failed
        redispatch, not silently reported as success."""
        watchdog.project_monitor.get_issue_column_sync.return_value = "Code Review"
        watchdog.project_monitor.trigger_agent_for_status.return_value = None

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()), \
             patch("services.work_execution_state.work_execution_tracker"):
            result = watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        assert result is False

    def test_closed_issue_releases_lock_instead_of_dispatching(self, watchdog):
        """Regression test for PR #110 review round 2, finding A: when the
        hand-rolled GraphQL query (which used to check state == 'CLOSED'
        upfront) was replaced with get_issue_column_sync() -- which does NOT
        filter closed issues -- this method stopped checking issue state at
        all, so a closed zombie issue's lock got silently released to
        whatever issue was queued behind it via trigger_agent_for_status's
        own closed-issue side effect. Must be caught explicitly, before ever
        calling trigger_agent_for_status, and treated as a clean success (not
        an alarm-worthy failure) since there is genuinely nothing to retry."""
        watchdog.project_monitor.get_issue_details.return_value = {"state": "CLOSED"}
        watchdog.lock_manager.release_lock = Mock(return_value=True)

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()):
            result = watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        assert result is True
        watchdog.lock_manager.release_lock.assert_called_once_with("proj", "SDLC Execution", 159)
        # Must never reach column resolution or dispatch for a closed issue.
        watchdog.project_monitor.get_issue_column_sync.assert_not_called()
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_closed_issue_returns_false_when_release_fails(self, watchdog):
        watchdog.project_monitor.get_issue_details.return_value = {"state": "CLOSED"}
        watchdog.lock_manager.release_lock = Mock(return_value=False)

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()):
            result = watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        assert result is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_returns_false_when_issue_state_cannot_be_fetched(self, watchdog):
        watchdog.project_monitor.get_issue_details.side_effect = RuntimeError("GitHub API down")

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()):
            result = watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        assert result is False
        watchdog.project_monitor.get_issue_column_sync.assert_not_called()
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_returns_false_when_no_project_monitor(self, watchdog):
        watchdog.project_monitor = None
        with patch("services.project_monitor.get_project_monitor", return_value=None):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False

    def test_returns_false_when_project_config_missing(self, watchdog):
        with patch("config.manager.config_manager.get_project_config", return_value=None):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_returns_false_when_no_current_column(self, watchdog):
        watchdog.project_monitor.get_issue_column_sync.return_value = None

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()

    def test_continues_dispatch_when_abandon_stale_entries_raises(self, watchdog):
        """abandon_stale_in_progress_entries is a best-effort call — if it
        raises, the redispatch attempt must still proceed (it's more useful
        to try the dispatch and possibly hit work_already_in_progress than
        to abandon the whole retry over a bookkeeping failure)."""
        watchdog.project_monitor.get_issue_column_sync.return_value = "Code Review"
        watchdog.project_monitor.trigger_agent_for_status.return_value = "task-abc"

        with patch("config.manager.config_manager.get_project_config", return_value=self._fake_project_config()), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker:
            mock_tracker.abandon_stale_in_progress_entries.side_effect = RuntimeError("boom")
            result = watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        assert result is True
        watchdog.project_monitor.trigger_agent_for_status.assert_called_once()

    def test_returns_false_on_unexpected_exception(self, watchdog):
        with patch("config.manager.config_manager.get_project_config", side_effect=RuntimeError("boom")):
            assert watchdog._redispatch_same_issue("proj", "SDLC Execution", 159) is False
        watchdog.project_monitor.trigger_agent_for_status.assert_not_called()


class TestAbandonStaleEntriesReason:
    """abandon_stale_in_progress_entries' persisted `error` text must not
    claim an orchestrator restart when called from self-heal, where none
    occurred (PR #110 review round 2, finding F)."""

    def test_redispatch_passes_a_self_heal_specific_reason(self, watchdog):
        cfg = Mock()
        cfg.github = {"org": "the-org", "repo": "the-repo"}
        watchdog.project_monitor.get_issue_column_sync.return_value = "Code Review"
        watchdog.project_monitor.trigger_agent_for_status.return_value = "task-abc"

        with patch("config.manager.config_manager.get_project_config", return_value=cfg), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker:
            watchdog._redispatch_same_issue("proj", "SDLC Execution", 159)

        reason = mock_tracker.abandon_stale_in_progress_entries.call_args.kwargs["reason"]
        # Must not be the default restart-recovery wording (it's fine for the
        # self-heal text to mention "not a restart" to clarify what it isn't;
        # what matters is it isn't the unqualified default claim).
        assert reason != "Orchestrator restarted without completing this execution."
        assert "self-heal" in reason.lower() or "watchdog" in reason.lower()


class TestProductionFallbackDependencies:
    """
    PipelineWatchdog.lock_manager and .project_monitor are both optional,
    injectable for tests, and fall back to the process-global singleton when
    not provided -- that fallback path is what production wiring actually
    relies on (scheduled_tasks.py's _cleanup_zombie_pipeline_runs constructs
    the watchdog with no project_monitor argument at all; get_pipeline_
    watchdog()'s own signature doesn't even expose one). Every other test in
    this file injects both, so this class is the only coverage of those two
    fallback branches actually being reached and used successfully.
    """

    def _watchdog_without_injected_deps(self):
        pipeline_run_manager = Mock()
        pipeline_run_manager.redis = FakeRedis()
        pipeline_run_manager.end_pipeline_run = Mock(return_value=True)
        return PipelineWatchdog(
            es_client=Mock(),
            pipeline_run_manager=pipeline_run_manager,
            # lock_manager and project_monitor deliberately omitted (None)
        )

    def test_cleanup_zombie_run_uses_global_lock_manager_when_none_injected(self):
        wd = self._watchdog_without_injected_deps()
        global_lock_mgr = Mock()
        global_lock_mgr.clear_retained_reason.return_value = True

        with patch(
            "services.pipeline_lock_manager.get_pipeline_lock_manager",
            return_value=global_lock_mgr,
        ), patch.object(wd, "_redispatch_same_issue", return_value=True) as redispatch, \
           patch.object(wd, "_notify_lock_stuck") as notify:
            wd._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T10:07:24Z",
            )

        global_lock_mgr.clear_retained_reason.assert_called_once_with(
            "proj", "SDLC Execution", 42
        )
        redispatch.assert_called_once()
        notify.assert_not_called()

    def test_redispatch_uses_global_project_monitor_when_none_injected(self):
        wd = self._watchdog_without_injected_deps()
        global_monitor = Mock()
        global_monitor.get_issue_column_sync.return_value = "Code Review"
        global_monitor.trigger_agent_for_status.return_value = "task-abc"
        cfg = Mock()
        cfg.github = {"org": "the-org", "repo": "the-repo"}

        with patch("services.project_monitor.get_project_monitor", return_value=global_monitor), \
             patch("config.manager.config_manager.get_project_config", return_value=cfg), \
             patch("services.work_execution_state.work_execution_tracker"):
            result = wd._redispatch_same_issue("proj", "SDLC Execution", 42)

        assert result is True
        global_monitor.trigger_agent_for_status.assert_called_once_with(
            project_name="proj",
            board_name="SDLC Execution",
            issue_number=42,
            status="Code Review",
            repository="the-repo",
            lock_already_acquired=True,
        )


class TestCleanupZombieRunWithRealLockManager:
    """
    Integration coverage using a REAL PipelineLockManager (YAML-only, no
    Redis) instead of a mock, and _redispatch_same_issue is NOT stubbed out
    -- only its own external boundary (ProjectMonitor) is. This is the one
    test in the suite that actually proves the property this PR exists for:
    the lock stays held by the SAME issue for the entire self-heal window,
    rather than just asserting the right mock methods were called with the
    right arguments.
    """

    def _real_lock_manager(self, tmp_path):
        from services.pipeline_lock_manager import PipelineLockManager
        return PipelineLockManager(state_dir=tmp_path, redis_client=None)

    def _watchdog(self, lock_mgr, project_monitor):
        pipeline_run_manager = Mock()
        pipeline_run_manager.redis = FakeRedis()
        pipeline_run_manager.end_pipeline_run = Mock(return_value=True)
        return PipelineWatchdog(
            es_client=Mock(),
            pipeline_run_manager=pipeline_run_manager,
            lock_manager=lock_mgr,
            project_monitor=project_monitor,
        )

    def test_lock_never_leaves_the_issue_across_a_full_self_heal_cycle(self, tmp_path):
        lock_mgr = self._real_lock_manager(tmp_path)
        lock_mgr._create_lock("proj", "SDLC Execution", 159)
        # Simulate what end_pipeline_run's outcome="failed" branch would have
        # already durably done before self-heal even starts.
        lock_mgr.mark_lock_failed("proj", "SDLC Execution", 159, reason="agent crashed")
        assert lock_mgr.get_lock("proj", "SDLC Execution").retained_reason is not None

        project_monitor = Mock()
        project_monitor.get_issue_column_sync.return_value = "Code Review"
        project_monitor.trigger_agent_for_status.return_value = "task-abc"
        wd = self._watchdog(lock_mgr, project_monitor)

        with patch("config.manager.config_manager.get_project_config") as get_cfg, \
             patch("services.work_execution_state.work_execution_tracker"), \
             patch.object(wd, "_notify_lock_stuck") as notify:
            cfg = Mock()
            cfg.github = {"org": "the-org", "repo": "the-repo"}
            get_cfg.return_value = cfg
            wd._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )

        lock = lock_mgr.get_lock("proj", "SDLC Execution")
        # The property that matters: still held by issue 159, no longer
        # durably retained, and never released to any other issue.
        assert lock.locked_by_issue == 159
        assert lock.lock_status == "locked"
        assert lock.retained_reason is None
        project_monitor.trigger_agent_for_status.assert_called_once()
        assert project_monitor.trigger_agent_for_status.call_args.kwargs["lock_already_acquired"] is True
        notify.assert_not_called()

        # And the lock genuinely refuses a different issue in the meantime --
        # this is the actual orphaning bug (incident e42ca133) this proves
        # can no longer happen.
        can_acquire, _reason = lock_mgr.try_acquire_lock("proj", "SDLC Execution", 999)
        assert can_acquire is False

    def test_redispatch_failure_restores_durable_retention_with_real_lock_manager(self, tmp_path):
        """When the redispatch itself fails after the durable mark was
        cleared, mark_lock_failed must actually re-apply against the real
        lock -- not just be called on a mock -- so a subsequent
        get_retained_reason() genuinely refuses re-dispatch."""
        lock_mgr = self._real_lock_manager(tmp_path)
        lock_mgr._create_lock("proj", "SDLC Execution", 159)
        lock_mgr.mark_lock_failed("proj", "SDLC Execution", 159, reason="agent crashed")

        project_monitor = Mock()
        project_monitor.get_issue_column_sync.return_value = "Code Review"
        project_monitor.trigger_agent_for_status.return_value = None  # no-op, per finding #1
        wd = self._watchdog(lock_mgr, project_monitor)

        with patch("config.manager.config_manager.get_project_config") as get_cfg, \
             patch("services.work_execution_state.work_execution_tracker"), \
             patch.object(wd, "_notify_lock_stuck") as notify:
            cfg = Mock()
            cfg.github = {"org": "the-org", "repo": "the-repo"}
            get_cfg.return_value = cfg
            wd._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )

        lock = lock_mgr.get_lock("proj", "SDLC Execution")
        assert lock.locked_by_issue == 159
        assert lock.retained_reason is not None
        assert lock_mgr.get_retained_reason("proj", "SDLC Execution", 159) is not None
        notify.assert_called_once()


class TestEndPipelineRunReturnsFalse:
    """
    end_pipeline_run() returning False (e.g. get_active_pipeline_run found no
    active run — a real race, not hypothetical) must be treated as requiring
    manual intervention with the lock's retention explicitly confirmed,
    regardless of whether self_heal was True or False (PR #110 review round
    2, finding C — the original fix only covered the self_heal=True branch).
    """

    def test_within_budget_marks_lock_failed_and_notifies(self, watchdog):
        watchdog.pipeline_run_manager.end_pipeline_run = Mock(return_value=False)
        watchdog.lock_manager.mark_lock_failed = Mock(return_value=True)

        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue") as redispatch:
            result = watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )

        assert result is False
        watchdog.lock_manager.clear_retained_reason.assert_not_called()
        redispatch.assert_not_called()
        watchdog.lock_manager.mark_lock_failed.assert_called_once()
        assert watchdog.lock_manager.mark_lock_failed.call_args.args[:3] == (
            "proj", "SDLC Execution", 159
        )
        notify.assert_called_once()

    def test_exceeded_budget_also_confirms_lock_retention(self, watchdog):
        """Before this fix, ended=False was only handled inside the
        self_heal branch -- the exceeded-budget/workspace-broken branches
        just trusted the lock was retained without checking, since
        end_pipeline_run's own internal mark_lock_failed call never ran when
        it returns False."""
        watchdog.pipeline_run_manager.end_pipeline_run = Mock(return_value=False)
        watchdog.lock_manager.mark_lock_failed = Mock(return_value=True)

        with patch.object(watchdog, "_notify_lock_stuck") as notify:
            for _ in range(ZOMBIE_AUTO_RETRY_LIMIT + 1):
                watchdog._cleanup_zombie_run(
                    pipeline_run_id="run-1",
                    project="proj",
                    board="SDLC Execution",
                    issue_number=159,
                    started_at="2026-08-10T10:07:24Z",
                )

        assert watchdog.lock_manager.mark_lock_failed.call_count == ZOMBIE_AUTO_RETRY_LIMIT + 1
        assert notify.call_count == ZOMBIE_AUTO_RETRY_LIMIT + 1

    def test_actively_resume_run_marks_lock_failed_and_notifies(self, watchdog):
        watchdog.pipeline_run_manager.end_pipeline_run = Mock(return_value=False)
        watchdog.lock_manager.mark_lock_failed = Mock(return_value=True)

        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue") as redispatch, \
             patch("services.review_cycle.review_cycle_executor") as mock_rc:
            mock_rc.active_cycles = {}
            mock_rc._load_active_cycles.return_value = []
            result = watchdog._actively_resume_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T15:00:00Z",
            )

        assert result is False
        watchdog.lock_manager.clear_retained_reason.assert_not_called()
        redispatch.assert_not_called()
        watchdog.lock_manager.mark_lock_failed.assert_called_once()
        notify.assert_called_once()


class TestSelfHealReturnValueReflectsOutcome:
    """
    _cleanup_zombie_run / _actively_resume_run now return bool (True only for
    a genuine clean self-heal), so check_for_zombie_runs' summary stats don't
    count a manual-intervention outcome as "cleaned up" (PR #110 review round
    2, finding H).
    """

    def test_cleanup_zombie_run_returns_false_when_manual_intervention_required(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck"), \
             patch.object(watchdog, "_is_workspace_git_broken", return_value=True):
            result = watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )
        assert result is False

    def test_cleanup_zombie_run_returns_true_on_clean_self_heal(self, watchdog):
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=True):
            result = watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )
        assert result is True
        notify.assert_not_called()

    def test_check_for_zombie_runs_only_counts_clean_self_heals(self, watchdog):
        watchdog.es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [{
                    "_source": {
                        "id": "run-1",
                        "project": "proj",
                        "issue_number": 159,
                        "board": "SDLC Execution",
                        "started_at": "2026-08-10T10:07:24Z",
                    }
                }],
            }
        }
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch.object(watchdog, "_check_for_agent_container", return_value=False), \
             patch.object(watchdog, "_cleanup_zombie_run", return_value=False) as cleanup:
            results = watchdog.check_for_zombie_runs()

        cleanup.assert_called_once()
        assert results["zombies_cleaned"] == 0
        assert results["details"][0]["action"] == "cleaned_up_requires_intervention"


class TestNotifyLockStuckReasonOverride:
    """
    Verified via the GitHub-comment path (services.github_integration.
    GitHubIntegration.post_comment) rather than the observability decision
    event: monitoring.observability_server.observability_server is a
    pre-existing broken import in _notify_lock_stuck (the real module lives
    at services/observability_server.py; there's no monitoring/observability_
    server.py at all) that predates this PR and is out of its scope -- it's
    silently swallowed by its own try/except at runtime, so the GitHub
    comment is the only channel this reason text actually reaches a human
    through in practice, which is what these tests exercise.
    """

    def _post_comment_message(self, watchdog, **kwargs):
        cfg = Mock()
        cfg.github = {"org": "the-org", "repo": "the-repo"}
        mock_github = Mock()
        mock_github.post_comment = AsyncMock(return_value=None)

        with patch("config.manager.config_manager.get_project_config", return_value=cfg), \
             patch("services.github_integration.GitHubIntegration", return_value=mock_github):
            watchdog._notify_lock_stuck("proj", "SDLC Execution", 42, "run-1", **kwargs)

        return mock_github.post_comment.call_args.args[1]

    def test_custom_reason_replaces_default_zombie_framing(self, watchdog):
        message = self._post_comment_message(
            watchdog, retry_count=0,
            reason="A completely custom explanation for this call.",
        )
        assert "A completely custom explanation for this call." in message
        assert "zombie-cleaned up" not in message

    def test_no_reason_falls_back_to_default_zombie_framing(self, watchdog):
        message = self._post_comment_message(watchdog, retry_count=3)
        assert "3 times" in message
        assert "auto-retry" in message.lower()


class TestCleanupOrderingBeforeRedispatch:
    """
    The stale-review-cycle-state cleanup must run BEFORE the self-heal
    redispatch, not after -- a synchronous redispatch can reach far enough to
    create a fresh ReviewCycleState for this same issue, and running the
    cleanup afterward could delete that brand-new state instead of a
    genuinely stale one (PR #110 review round 2, finding A/#3 regression
    risk -- this reordering itself was previously untested, so a future edit
    reverting the order would pass every other test in this file).
    """

    def test_review_cycle_cleanup_runs_before_redispatch_in_cleanup_zombie_run(self, watchdog):
        call_order = []

        def fake_load_active_cycles(project_name):
            call_order.append("review_cycle_cleanup")
            return []

        def fake_redispatch(project, board, issue_number):
            call_order.append("redispatch")
            return True

        with patch.object(watchdog, "_redispatch_same_issue", side_effect=fake_redispatch), \
             patch("services.review_cycle.review_cycle_executor") as mock_rc:
            mock_rc.active_cycles = {}
            mock_rc._load_active_cycles.side_effect = fake_load_active_cycles
            watchdog._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=159,
                started_at="2026-08-10T10:07:24Z",
            )

        assert call_order == ["review_cycle_cleanup", "redispatch"]

    def test_review_cycle_cleanup_runs_before_redispatch_in_actively_resume_run(self, watchdog):
        call_order = []

        def fake_load_active_cycles(project_name):
            call_order.append("review_cycle_cleanup")
            return []

        def fake_redispatch(project, board, issue_number):
            call_order.append("redispatch")
            return True

        with patch.object(watchdog, "_redispatch_same_issue", side_effect=fake_redispatch), \
             patch("services.review_cycle.review_cycle_executor") as mock_rc:
            mock_rc.active_cycles = {}
            mock_rc._load_active_cycles.side_effect = fake_load_active_cycles
            watchdog._actively_resume_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T15:00:00Z",
            )

        assert call_order == ["review_cycle_cleanup", "redispatch"]


class TestActivelyResumeRunWithRealLockManager:
    """Mirror of TestCleanupZombieRunWithRealLockManager for the breaker-
    freeze active-resume path (PR #110 review round 2, finding E gap: only
    _cleanup_zombie_run had real-lock-manager integration coverage)."""

    def _real_lock_manager(self, tmp_path):
        from services.pipeline_lock_manager import PipelineLockManager
        return PipelineLockManager(state_dir=tmp_path, redis_client=None)

    def _watchdog(self, lock_mgr, project_monitor):
        pipeline_run_manager = Mock()
        pipeline_run_manager.redis = FakeRedis()
        pipeline_run_manager.end_pipeline_run = Mock(return_value=True)
        return PipelineWatchdog(
            es_client=Mock(),
            pipeline_run_manager=pipeline_run_manager,
            lock_manager=lock_mgr,
            project_monitor=project_monitor,
        )

    def test_lock_never_leaves_the_issue_across_active_resume(self, tmp_path):
        lock_mgr = self._real_lock_manager(tmp_path)
        lock_mgr._create_lock("proj", "SDLC Execution", 42)
        lock_mgr.mark_lock_failed("proj", "SDLC Execution", 42, reason="frozen by breaker")

        project_monitor = Mock()
        project_monitor.get_issue_details.return_value = {"state": "OPEN"}
        project_monitor.get_issue_column_sync.return_value = "Code Review"
        project_monitor.trigger_agent_for_status.return_value = "task-abc"
        wd = self._watchdog(lock_mgr, project_monitor)

        with patch("config.manager.config_manager.get_project_config") as get_cfg, \
             patch("services.work_execution_state.work_execution_tracker"), \
             patch("services.review_cycle.review_cycle_executor") as mock_rc, \
             patch.object(wd, "_notify_lock_stuck") as notify:
            mock_rc.active_cycles = {}
            mock_rc._load_active_cycles.return_value = []
            cfg = Mock()
            cfg.github = {"org": "the-org", "repo": "the-repo"}
            get_cfg.return_value = cfg
            result = wd._actively_resume_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T15:00:00Z",
            )

        assert result is True
        lock = lock_mgr.get_lock("proj", "SDLC Execution")
        assert lock.locked_by_issue == 42
        assert lock.retained_reason is None
        notify.assert_not_called()
        can_acquire, _reason = lock_mgr.try_acquire_lock("proj", "SDLC Execution", 999)
        assert can_acquire is False
