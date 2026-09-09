"""
Regression tests for #148 (from #140 item 8): ProjectWorkspaceManager.
initialize_all_projects() must not report a project it could not even attempt
as "confirmed, no dev environment setup needed".

initialize_project() acquires the project_checkout lock (#54) around the
clone/update, with a deliberately short 120s timeout, and raises
ProjectCheckoutLockTimeoutError if a stale lock left by a crashed prior process
still owns it. That used to be caught alongside every genuine clone failure and
recorded as needs_setup[project] = False — which main.py's startup
dispatch-queuing loop reads as an assertion that the project is fine. It is
now SetupStatus.UNKNOWN: distinguishable and loudly logged, and read at every
call site by member (`is SetupStatus.NEEDED`) rather than by truthiness, so
the three states cannot silently re-collapse into two.

Without the fix these tests fail: initialize_all_projects() returns bare
booleans, so UNKNOWN is indistinguishable from a confirmed NOT_NEEDED.
"""

import logging
import pytest
from unittest.mock import MagicMock, patch

from services.project_workspace import (
    ProjectWorkspaceManager,
    SetupStatus,
    resolve_setup_queue,
)
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError


@pytest.fixture
def manager(tmp_path):
    return ProjectWorkspaceManager(workspace_root=tmp_path)


def _project_config():
    config = MagicMock()
    config.github = {'repo_url': 'git@github.com:test-org/test-repo.git'}
    return config


def _run(manager, projects, initialize_side_effect, dockerfile_projects=()):
    """
    Drive initialize_all_projects() with config_manager and initialize_project()
    stubbed. `initialize_side_effect` is passed straight to the
    initialize_project mock (a list of per-project results/exceptions).
    `dockerfile_projects` names the projects whose base clone has a
    Dockerfile.agent on disk.
    """
    for project_name in dockerfile_projects:
        project_dir = manager.workspace_root / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / 'Dockerfile.agent').write_text("FROM scratch\n")

    with patch('services.project_workspace.config_manager') as mock_config:
        mock_config.list_visible_projects.return_value = list(projects)
        mock_config.get_project_config.side_effect = lambda name: _project_config()

        with patch.object(
            manager, 'initialize_project', side_effect=initialize_side_effect
        ):
            return manager.initialize_all_projects()


class TestSetupStatusEnum:
    def test_defines_no_bool_so_truthiness_cannot_conflate_states(self):
        """
        The type must not carry a __bool__: one that was truthy only for NEEDED
        made `if status:` give UNKNOWN and NOT_NEEDED the same answer at the only
        places the value is read, which is exactly the conflation the enum exists
        to remove. Callers name the member instead.
        """
        assert '__bool__' not in SetupStatus.__dict__
        # Enum members are truthy by default, so a call site that still tested
        # truthiness would now queue setup for every project — loudly wrong rather
        # than silently wrong, and caught by TestResolveSetupQueue below (which
        # drives the only reader of these values, so a reverted `if needs_setup:`
        # fails a test instead of quietly rebuilding every project's image on
        # every startup).
        assert bool(SetupStatus.UNKNOWN) is True

    def test_members_are_distinct(self):
        assert len({SetupStatus.NEEDED, SetupStatus.NOT_NEEDED, SetupStatus.UNKNOWN}) == 3


class TestResolveSetupQueue:
    """
    The startup call site itself: which projects actually get a HIGH-priority
    dev_environment_setup task queued. This lived inline in main.py's startup
    coroutine, reachable by no test — so neither the `is SetupStatus.NEEDED`
    member test nor the UNKNOWN→NEEDED upgrade on a missing Docker image had any
    coverage, and the enum's deliberate truthiness made reverting the former to
    the idiomatic `if needs_setup:` both legal and invisible.
    """

    @staticmethod
    def _image(*present):
        """verify_and_update_status() stub: True only for the named projects."""
        present = set(present)
        return lambda project_name: project_name in present

    def test_confirmed_not_needed_with_a_present_image_queues_nothing(self):
        assert resolve_setup_queue(
            {'alpha': SetupStatus.NOT_NEEDED}, self._image('alpha')
        ) == []

    def test_unknown_with_a_present_image_queues_nothing(self):
        """UNKNOWN is not an assertion that setup is needed — and with the image
        verifiably present there is no independent reason to rebuild it."""
        assert resolve_setup_queue(
            {'alpha': SetupStatus.UNKNOWN}, self._image('alpha')
        ) == []

    def test_unknown_with_a_missing_image_is_upgraded_to_needed(self):
        """The independent, positive observation about the Docker image stands on
        its own: a verifiably missing image needs setup regardless of whether the
        checkout could be inspected this startup."""
        assert resolve_setup_queue(
            {'alpha': SetupStatus.UNKNOWN}, self._image()
        ) == ['alpha']

    def test_not_needed_with_a_missing_image_is_also_upgraded(self):
        assert resolve_setup_queue(
            {'alpha': SetupStatus.NOT_NEEDED}, self._image()
        ) == ['alpha']

    def test_needed_always_queues(self):
        assert resolve_setup_queue(
            {'alpha': SetupStatus.NEEDED}, self._image('alpha')
        ) == ['alpha']

    def test_truthiness_would_queue_every_project_so_the_member_test_is_load_bearing(self):
        """
        The regression this guards: `if needs_setup:` reads as fine and is legal
        (enum members are truthy), and would queue a HIGH-priority setup task for
        every configured project on every startup — each acquiring
        dev_container_build and rebuilding Dockerfile.agent.
        """
        statuses = {
            'alpha': SetupStatus.NEEDED,
            'beta': SetupStatus.NOT_NEEDED,
            'gamma': SetupStatus.UNKNOWN,
        }
        # Every image present, so nothing is upgraded — only 'alpha' is a
        # confirmed NEEDED. Truthiness would return all three.
        assert resolve_setup_queue(
            statuses, self._image('alpha', 'beta', 'gamma')
        ) == ['alpha']

    def test_the_image_check_runs_once_per_project(self):
        """It updates dev-container state as a side effect, so it must not be
        skipped for a NEEDED project nor called twice for any project."""
        seen = []

        def image_verified(project_name):
            seen.append(project_name)
            return True

        resolve_setup_queue(
            {'alpha': SetupStatus.NEEDED, 'beta': SetupStatus.UNKNOWN}, image_verified
        )
        assert seen == ['alpha', 'beta']


class TestConfirmedOutcomes:
    def test_newly_cloned_project_is_needed(self, manager):
        result = _run(manager, ['alpha'], initialize_side_effect=[True])
        assert result == {'alpha': SetupStatus.NEEDED}

    def test_existing_project_without_dockerfile_is_needed(self, manager):
        result = _run(manager, ['alpha'], initialize_side_effect=[False])
        assert result == {'alpha': SetupStatus.NEEDED}

    def test_existing_project_with_dockerfile_is_not_needed(self, manager):
        result = _run(
            manager, ['alpha'], initialize_side_effect=[False], dockerfile_projects=['alpha']
        )
        assert result == {'alpha': SetupStatus.NOT_NEEDED}


class TestLockTimeoutIsUnknownNotFalse:
    def test_lock_timeout_reports_unknown(self, manager):
        result = _run(
            manager,
            ['alpha'],
            initialize_side_effect=[ProjectCheckoutLockTimeoutError(
                "Could not acquire 'project_checkout' lock for project 'alpha'"
            )],
        )

        assert result == {'alpha': SetupStatus.UNKNOWN}
        # Not a confirmed NOT_NEEDED, and not a NEEDED either — so main.py's
        # `is SetupStatus.NEEDED` queues nothing on the strength of a non-answer,
        # while the state stays distinguishable to any caller that wants it.
        assert result['alpha'] is not SetupStatus.NOT_NEEDED
        assert result['alpha'] is not SetupStatus.NEEDED

    def test_unknown_is_distinguishable_from_a_confirmed_not_needed(self, manager):
        """The whole point: two projects that both queue no setup task, for two
        categorically different reasons, must not look the same to a caller."""
        result = _run(
            manager,
            ['alpha', 'beta'],
            initialize_side_effect=[False, ProjectCheckoutLockTimeoutError("busy")],
            dockerfile_projects=['alpha'],
        )

        assert result['alpha'] is SetupStatus.NOT_NEEDED
        assert result['beta'] is SetupStatus.UNKNOWN
        # Both queue no setup task, for two categorically different reasons.
        assert result['alpha'] is not SetupStatus.NEEDED
        assert result['beta'] is not SetupStatus.NEEDED

    def test_genuine_initialization_failure_also_reports_unknown(self, manager):
        """A clone failure was never a confirmed 'no setup needed' either."""
        result = _run(
            manager, ['alpha'], initialize_side_effect=[RuntimeError("clone failed")]
        )
        assert result == {'alpha': SetupStatus.UNKNOWN}

    def test_one_projects_failure_does_not_affect_the_others(self, manager):
        result = _run(
            manager,
            ['alpha', 'beta', 'gamma'],
            initialize_side_effect=[
                ProjectCheckoutLockTimeoutError("busy"),
                True,
                False,
            ],
            dockerfile_projects=['gamma'],
        )

        assert result == {
            'alpha': SetupStatus.UNKNOWN,
            'beta': SetupStatus.NEEDED,
            'gamma': SetupStatus.NOT_NEEDED,
        }

    def test_unknown_projects_are_reported_in_a_summary_warning(self, manager, caplog):
        with caplog.at_level(logging.WARNING, logger='services.project_workspace'):
            _run(
                manager,
                ['alpha', 'beta'],
                initialize_side_effect=[ProjectCheckoutLockTimeoutError("busy"), True],
            )

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any('alpha' in message and 'never determined' in message for message in warnings)
        assert not any('beta' in message for message in warnings)

    def test_no_summary_warning_when_every_project_initialized(self, manager, caplog):
        with caplog.at_level(logging.WARNING, logger='services.project_workspace'):
            _run(manager, ['alpha'], initialize_side_effect=[True])

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
