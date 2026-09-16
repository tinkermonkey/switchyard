"""
Three places where the orchestrator boots, something is wrong, and nobody finds
out (#257, #137, #136).

Each is a different failure, but the shape is the same: a condition that is
detectable at startup, that does not clear on its own, and whose only trace was
a log line an operator would have to already be looking for.

  #257  the base image tag had been taken by another compose project, and
        nothing asked until a pipeline run failed on a live issue
  #137  a review cycle that could not be resumed left no GitHub-visible signal
        and was never cleaned up, because its status never reached 'completed'
  #136  a base clone git cannot read fell into "update it" or "clone over it",
        both of which fail naming the symptom rather than the cause
"""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.project_workspace import ProjectWorkspaceManager


class TestTheBaseImageIsCheckedAtStartup:
    """#257. #254 put this check on the dispatch path; dispatch-time was the
    first time anything asked."""

    def test_a_hijacked_tag_is_reported_without_stopping_startup(self):
        """Warns rather than failing, unlike the sibling dev-container guard.

        A tag collision is a RUNTIME condition that a rebuild clears and that
        can be briefly transient, and #254's dispatch guard already refuses the
        launch and says why. Refusing to boot would also ground every agent
        that does NOT need the base image, and take the observability server's
        view of the system down at the moment an operator needs it.
        """
        from main import _report_base_image_identity
        from claude.docker_runner import BaseImageIdentityError

        logger = MagicMock()
        with patch('claude.docker_runner.DockerAgentRunner._assert_base_image_is_ours',
                   side_effect=BaseImageIdentityError("not built by switchyard")), \
             patch('claude.docker_runner.DockerAgentRunner.find_dangling_base_images',
                   return_value=[]):
            result = _report_base_image_identity(logger)

        assert result is False
        assert logger.log_error.called
        logged = ' '.join(str(c) for c in logger.log_error.call_args_list)
        assert 'not built by switchyard' in logged, "the operator needs the cause"
        assert 'Continuing startup' in logged, "and needs to know it did not stop"

    def test_a_dangling_image_is_named_so_the_tag_can_be_recovered(self):
        """#251's second failure shape, and the reason this is a startup
        concern at all: the real image is not deleted, it is left with
        `tags: []` -- alive only because the running container pins it by id,
        and one `docker image prune` from being collected. Recovering it needs
        that id, which is exactly what an operator does not have."""
        from main import _report_base_image_identity
        from claude.docker_runner import BaseImageIdentityError

        logger = MagicMock()
        with patch('claude.docker_runner.DockerAgentRunner._assert_base_image_is_ours',
                   side_effect=BaseImageIdentityError("missing the label")), \
             patch('claude.docker_runner.DockerAgentRunner.find_dangling_base_images',
                   return_value=['d308aef0b077']):
            _report_base_image_identity(logger)

        logged = ' '.join(str(c) for c in logger.log_error.call_args_list)
        assert 'd308aef0b077' in logged
        assert 'docker tag d308aef0b077' in logged, "give the command, not a hint"
        assert 'prune' in logged, "and say what will destroy it"

    def test_several_candidates_are_listed_rather_than_one_being_guessed(self):
        """Every <project>-agent image is built FROM the orchestrator and
        inherits the label, so superseded agent builds accumulate here -- nine
        of them on the box this was written on. Naming one at random would be
        worse than naming none: retagging the wrong image reproduces #251
        exactly, with switchyard's own blessing."""
        from main import _report_base_image_identity
        from claude.docker_runner import BaseImageIdentityError

        ids = ['ae57415354d2', 'f4a5e826fb31', '6b2a22800ffa']
        logger = MagicMock()
        with patch('claude.docker_runner.DockerAgentRunner._assert_base_image_is_ours',
                   side_effect=BaseImageIdentityError("missing the label")), \
             patch('claude.docker_runner.DockerAgentRunner.find_dangling_base_images',
                   return_value=ids):
            _report_base_image_identity(logger)

        logged = ' '.join(str(c) for c in logger.log_error.call_args_list)
        for image_id in ids:
            assert image_id in logged, "list every candidate"
        assert 'docker tag ae57415354d2' not in logged, (
            "must not pick one -- retagging the wrong image IS #251"
        )
        assert 'Config.Cmd' in logged, "tell them how to identify the right one"

    def test_no_dangling_image_says_so_rather_than_implying_recovery(self):
        """Silence here would read as "there was nothing to recover", when it
        actually means the real image may already be gone."""
        from main import _report_base_image_identity
        from claude.docker_runner import BaseImageIdentityError

        logger = MagicMock()
        with patch('claude.docker_runner.DockerAgentRunner._assert_base_image_is_ours',
                   side_effect=BaseImageIdentityError("missing the label")), \
             patch('claude.docker_runner.DockerAgentRunner.find_dangling_base_images',
                   return_value=[]):
            _report_base_image_identity(logger)

        logged = ' '.join(str(c) for c in logger.log_error.call_args_list)
        assert 'already have been collected' in logged
        assert 'docker compose build orchestrator' in logged

    def test_a_good_image_reports_success_and_raises_nothing(self):
        from main import _report_base_image_identity

        logger = MagicMock()
        with patch('claude.docker_runner.DockerAgentRunner._assert_base_image_is_ours'):
            assert _report_base_image_identity(logger) is True
        assert not logger.log_error.called

    def test_it_is_actually_called_during_startup(self):
        """The function being correct is not the same as it running.

        Deleting the call from main() left every other test in this class
        green, which is the same blind spot TestStartupEnforcement records for
        its own sibling guard. Source inspection is a weak check -- it cannot
        tell a live call from one inside a dead branch -- but main() is a
        400-line async entrypoint that cannot be driven in a unit test, and a
        weak check on the wiring beats none while the behaviour above is
        pinned properly.
        """
        import inspect
        import main as main_module

        source = inspect.getsource(main_module.main)
        assert '_report_base_image_identity(logger)' in source, (
            "the startup check is no longer wired into main()"
        )

    def test_the_dangling_probe_never_raises(self):
        """It only ever decorates a diagnosis that has already been decided, so
        a docker hiccup must not turn a clear message into a stack trace."""
        from claude.docker_runner import DockerAgentRunner

        with patch('claude.docker_runner.subprocess.run',
                   side_effect=OSError("docker gone")):
            assert DockerAgentRunner.find_dangling_base_images() == []

        with patch('claude.docker_runner.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='docker', timeout=10)):
            assert DockerAgentRunner.find_dangling_base_images() == []


class TestAnUnreadableBaseCloneIsRefusedNotBlundered:
    """#136, the base-clone analog of #250."""

    def _manager(self, tmp_path):
        m = ProjectWorkspaceManager.__new__(ProjectWorkspaceManager)
        m.workspace_root = tmp_path
        return m

    def _config(self):
        cfg = MagicMock()
        cfg.github = {'repo_url': 'git@github.com:acme/thing.git', 'branch': 'main'}
        return cfg

    def test_an_unidentifiable_base_clone_is_not_cloned_over_or_updated(self, tmp_path):
        """Both original branches assume the directory is a working clone or
        absent. For one git cannot read, the `if` runs fetch/checkout against
        broken metadata and the `else` runs `git clone` into a non-empty
        directory -- "destination path already exists and is not an empty
        directory". Both name the symptom, not the cause."""
        manager = self._manager(tmp_path)
        project_dir = tmp_path / 'thing'
        project_dir.mkdir()
        (project_dir / '.git').write_text("gitdir: /nonexistent/nowhere\n")
        (project_dir / 'unpushed.py').write_text("# work\n")

        from contextlib import contextmanager

        @contextmanager
        def _no_lock(*a, **k):
            yield

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _no_lock), \
             patch.object(ProjectWorkspaceManager, '_clone_repository') as clone, \
             patch.object(ProjectWorkspaceManager, '_update_repository') as update, \
             patch('services.project_workspace.subprocess.run',
                   return_value=MagicMock(returncode=128, stdout='', stderr='fatal')):
            with pytest.raises(RuntimeError, match="cannot identify it as a repository"):
                manager.initialize_project('thing', self._config())

        assert not clone.called, "never clone over a directory that may hold work"
        assert not update.called, "and never fetch into metadata git cannot read"
        # Untouched. This is the guarantee, and it is the whole point.
        assert (project_dir / 'unpushed.py').read_text() == "# work\n"

    def test_the_refusal_says_what_to_do_and_what_happens_next(self, tmp_path):
        manager = self._manager(tmp_path)
        project_dir = tmp_path / 'thing'
        project_dir.mkdir()
        (project_dir / '.git').write_text("gitdir: /nonexistent\n")
        (project_dir / 'file.py').write_text("x")

        from contextlib import contextmanager

        @contextmanager
        def _no_lock(*a, **k):
            yield

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _no_lock), \
             patch('services.project_workspace.subprocess.run',
                   return_value=MagicMock(returncode=128, stdout='', stderr='fatal')):
            with pytest.raises(RuntimeError) as exc:
                manager.initialize_project('thing', self._config())

        message = str(exc.value)
        assert 'may hold work that was never pushed' in message
        assert 're-clone' in message, "say that recovery is automatic once moved"

    def test_a_healthy_clone_still_takes_the_update_path(self, tmp_path):
        """Control: the guard must not stand between every project and its
        ordinary startup update."""
        manager = self._manager(tmp_path)
        project_dir = tmp_path / 'thing'
        project_dir.mkdir()
        (project_dir / '.git').mkdir()
        (project_dir / 'file.py').write_text("x")

        from contextlib import contextmanager

        @contextmanager
        def _no_lock(*a, **k):
            yield

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _no_lock), \
             patch.object(ProjectWorkspaceManager, '_update_repository') as update, \
             patch.object(ProjectWorkspaceManager, '_ensure_ssh_remote'), \
             patch('services.project_workspace.subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='/some/.git\n', stderr='')):
            was_cloned = manager.initialize_project('thing', self._config())

        assert was_cloned is False
        assert update.called

    def test_an_absent_directory_still_clones(self, tmp_path):
        """Control: nothing there is not the same as something unreadable."""
        manager = self._manager(tmp_path)

        from contextlib import contextmanager

        @contextmanager
        def _no_lock(*a, **k):
            yield

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _no_lock), \
             patch.object(ProjectWorkspaceManager, '_clone_repository') as clone, \
             patch.object(ProjectWorkspaceManager, '_ensure_ssh_remote'):
            was_cloned = manager.initialize_project('thing', self._config())

        assert was_cloned is True
        assert clone.called


class TestTheDeadProjectManagerIsGone:
    """#136's other half.

    The issue described a corruption gap in services/project_manager.py. By the
    time it was addressed the module had no importers at all -- its only caller,
    scripts/setup_projects.py, no longer existed, and config/projects.yaml (which
    its constructor reads) had been replaced by config/projects/<name>.yaml. It
    could not have been constructed, let alone reached the gap. Removed rather
    than fixed: there is no defect in code that cannot run, and leaving a second,
    older clone-or-update implementation around invites someone to revive it.
    """

    def test_it_is_not_importable(self):
        with pytest.raises(ImportError):
            import services.project_manager  # noqa: F401


def _cycle_state():
    from services.review_cycle import ReviewCycleState
    return ReviewCycleState(
        issue_number=903,
        repository='acme/thing',
        maker_agent='senior_software_engineer',
        reviewer_agent='code_reviewer',
        max_iterations=3,
        project_name='thing',
        board_name='dev_workflow',
    )


def _executor():
    """A ReviewCycleExecutor with only the collaborators these paths touch."""
    from services.review_cycle import ReviewCycleExecutor
    ex = ReviewCycleExecutor.__new__(ReviewCycleExecutor)
    ex.decision_events = MagicMock()
    ex._save_cycle_state = MagicMock()
    ex._get_github_integration = MagicMock(
        return_value=MagicMock(post_issue_comment=AsyncMock())
    )
    return ex


class TestAnUnresumableCycleEscalates:
    """#137. resume_active_cycles() runs at every startup, and both
    _continue_cycle_from_* ended in a bare logger.error with no re-raise and no
    escalation -- so the cycle kept whatever status it had, never 'completed',
    and pipeline_watchdog's zombie check read that as legitimately in flight.
    A permanent, invisible stall that failed identically on every restart."""

    @pytest.mark.asyncio
    async def test_it_reaches_awaiting_human_feedback_so_the_watchdog_can_see_it(self):
        ex = _executor()
        state = _cycle_state()
        state.status = 'reviewer_working'

        with patch('subprocess.run'), \
             patch('services.pipeline_run.get_pipeline_run_manager'):
            await ex._escalate_resume_failure(state, RuntimeError("worktree unreadable"))

        assert state.status == 'awaiting_human_feedback', (
            "any status but this leaves the watchdog treating the cycle as live"
        )
        assert state.escalation_time is not None
        assert ex._save_cycle_state.called, "an unsaved status change survives nothing"

    @pytest.mark.asyncio
    async def test_the_operator_gets_the_cause_on_the_issue(self):
        ex = _executor()
        state = _cycle_state()

        with patch('subprocess.run'), \
             patch('services.pipeline_run.get_pipeline_run_manager'):
            await ex._escalate_resume_failure(
                state, RuntimeError("git cannot identify it as a worktree")
            )

        github = ex._get_github_integration.return_value
        assert github.post_issue_comment.called, "a log line is not a signal"
        body = github.post_issue_comment.call_args.args[1]
        assert 'git cannot identify it as a worktree' in body
        assert 'RuntimeError' in body
        assert 'unreadable-worktrees' in body, (
            "name where #250's sweep puts the work, since that is the likely cause"
        )

    @pytest.mark.asyncio
    async def test_it_is_labelled_for_human_review(self):
        ex = _executor()
        state = _cycle_state()

        with patch('subprocess.run') as run, \
             patch('services.pipeline_run.get_pipeline_run_manager'):
            await ex._escalate_resume_failure(state, RuntimeError("boom"))

        argv = run.call_args.args[0]
        assert 'needs-human-review' in argv

    @pytest.mark.asyncio
    async def test_a_failing_escalation_does_not_replace_the_original_error(self, caplog):
        """Both call sites are inside an `except` that already logged the real
        cause. Raising here would swap the diagnosis for a secondary one and, in
        resume_active_cycles()'s loop, abandon the project's remaining cycles."""
        ex = _executor()
        state = _cycle_state()

        with patch.object(type(ex), '_escalate_resume_failure',
                          side_effect=RuntimeError("github unreachable")):
            await ex._escalate_resume_failure_safely(
                state, ValueError("the original cause")
            )

        logged = caplog.text
        assert 'the original cause' in logged, "the real failure must survive"
        assert 'github unreachable' in logged

    @pytest.mark.asyncio
    async def test_a_cancelled_escalation_is_not_swallowed(self):
        """The wrapper turns failures into log lines, which is right for a
        GitHub outage and wrong for a shutdown -- swallowing cancellation would
        let a stopping orchestrator keep working through cycles."""
        from claude.claude_integration import CancellationError

        ex = _executor()
        state = _cycle_state()

        with patch.object(type(ex), '_escalate_resume_failure',
                          side_effect=CancellationError("stopping")):
            with pytest.raises(CancellationError):
                await ex._escalate_resume_failure_safely(state, RuntimeError("boom"))

    @pytest.mark.asyncio
    async def test_the_review_handler_escalates(self):
        """The handler #137 was filed against. get_or_create_epic_worktree()
        raising on a corrupted worktree lands exactly here."""
        ex = _executor()
        ex._escalate_resume_failure_safely = AsyncMock()
        ex.review_parser = MagicMock(
            parse_review=MagicMock(side_effect=RuntimeError("worktree unreadable"))
        )
        state = _cycle_state()
        state.current_iteration = 1
        state.review_outputs = [{'iteration': 1, 'output': 'some review'}]

        await ex._continue_cycle_from_review(state, 'acme')

        assert ex._escalate_resume_failure_safely.called, (
            "this is the silent, permanent stall #137 describes"
        )
        assert ex._escalate_resume_failure_safely.call_args.args[1].args[0] == (
            "worktree unreadable"
        ), "the original cause must be what gets escalated"

    @pytest.mark.asyncio
    async def test_the_maker_handler_escalates_too(self):
        """_continue_cycle_from_maker had the same bare handler, and
        resume_active_cycles() dispatches to BOTH."""
        ex = _executor()
        ex._escalate_resume_failure_safely = AsyncMock()
        state = _cycle_state()
        ex._save_cycle_state = MagicMock(side_effect=RuntimeError("disk full"))

        await ex._continue_cycle_from_maker(state, 'acme')

        assert ex._escalate_resume_failure_safely.called, (
            "a resume failure here was silent in exactly the same way"
        )

    @pytest.mark.asyncio
    async def test_the_maker_handler_propagates_cancellation(self):
        """The sibling handler always exempted CancellationError; this one
        never did, so a shutdown mid-resume was logged as a cycle failure --
        and now would also escalate one to a human."""
        from claude.claude_integration import CancellationError

        ex = _executor()
        ex._escalate_resume_failure_safely = AsyncMock()
        state = _cycle_state()
        ex._save_cycle_state = MagicMock(side_effect=CancellationError("stopping"))

        with pytest.raises(CancellationError):
            await ex._continue_cycle_from_maker(state, 'acme')

        assert not ex._escalate_resume_failure_safely.called, (
            "a shutdown must not raise a human escalation"
        )
