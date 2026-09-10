"""
The producing half of the org.switchyard.base_clone contract (#171 review).

services/project_checkout_lock.project_has_live_agent_container() -- the
survivor probe main.py hands recover_orphaned_resource_locks() at startup --
treats an explicit 'false' on this label as proof that a container which
outlived the previous orchestrator process cannot be working inside the
project's shared base clone, and therefore cannot be the live holder of a
surviving project_checkout lock. That is the only value that can dispossess a
holder, so these tests pin both directions of who stamps it:

  - claude/docker_runner.py stamps it from the SAME is_base_clone_dir()
    predicate claude_integration.py uses (on the same project_dir) to decide
    whether to take the lock around that container run at all -- so the label
    and the lock cannot disagree -- and fails closed to 'true' when it cannot
    tell.
  - services/project_monitor.py's repair-cycle launch stamps 'true'
    unconditionally: that container mounts the base clone at its conventional
    /workspace/<project> path regardless of where its own project_dir points.

The consuming half is covered in
tests/unit/services/test_project_checkout_lock.py
(TestProjectHasLiveAgentContainer).
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude.docker_runner import DockerAgentRunner

from services.project_checkout_lock import BASE_CLONE_LABEL


def _label_value(cmd, label):
    """The value docker would receive for `label` in a built `docker run` argv."""
    for i, arg in enumerate(cmd):
        if arg == '--label' and cmd[i + 1].startswith(f'{label}='):
            return cmd[i + 1][len(label) + 1:]
    return None


class TestDockerRunnerStampsTheBaseCloneLabel:

    def test_a_base_clone_run_is_labelled_true(self):
        with patch('services.project_workspace.workspace_manager') as wm:
            wm.is_base_clone_dir.return_value = True
            value = DockerAgentRunner._is_base_clone_label(
                {'project': 'proj'}, Path('/workspace/proj')
            )

        assert value == 'true'
        wm.is_base_clone_dir.assert_called_once_with('proj', Path('/workspace/proj'))

    def test_an_epic_worktree_run_is_labelled_false(self):
        """The whole point: this is the container the probe must ignore."""
        with patch('services.project_workspace.workspace_manager') as wm:
            wm.is_base_clone_dir.return_value = False
            value = DockerAgentRunner._is_base_clone_label(
                {'project': 'proj'}, Path('/workspace/proj/.orchestrator/worktrees/proj/epic-1')
            )

        assert value == 'false'

    def test_an_unresolvable_project_fails_closed(self):
        assert DockerAgentRunner._is_base_clone_label({}, Path('/workspace/proj')) == 'true'

    def test_a_raising_predicate_fails_closed(self):
        with patch('services.project_workspace.workspace_manager') as wm:
            wm.is_base_clone_dir.side_effect = RuntimeError("boom")
            value = DockerAgentRunner._is_base_clone_label(
                {'project': 'proj'}, Path('/workspace/proj')
            )

        assert value == 'true'

    def test_the_built_docker_command_carries_it(self, tmp_path):
        """Integration-level: the label actually reaches the argv, not just the
        helper. A worktree-scoped run must come out 'false' end to end."""
        project_dir = tmp_path / 'workspace' / 'proj'
        (project_dir / '.git').mkdir(parents=True)

        runner = DockerAgentRunner()
        agent_config = MagicMock()
        agent_config.filesystem_write_allowed = False

        with patch.dict('os.environ', {'ORCHESTRATOR_ROOT': str(tmp_path / 'root')}), \
                patch.object(DockerAgentRunner, '_detect_host_workspace_path',
                             return_value='/host/workspace'), \
                patch.object(DockerAgentRunner, '_detect_host_home_path',
                             return_value='/host/home'), \
                patch('config.manager.config_manager.get_project_agent_config',
                      return_value=agent_config), \
                patch('claude.environment.ClaudeEnvironmentBuilder') as env_builder_cls, \
                patch('services.project_workspace.workspace_manager') as wm:
            env_builder_cls.return_value.build.return_value = MagicMock(
                to_docker_env_args=lambda: []
            )
            wm.is_base_clone_dir.return_value = False

            cmd, _image = runner._build_docker_command(
                container_name='test-container',
                project_dir=project_dir,
                mcp_config_path=None,
                context={
                    'agent': 'senior_software_engineer',
                    'project': 'proj',
                    'task_id': 'task-1',
                },
            )

        assert _label_value(cmd, BASE_CLONE_LABEL) == 'false'
        # Still the project-scoped label the probe filters on, unchanged.
        assert _label_value(cmd, 'org.switchyard.project') == 'proj'


class TestRepairCycleContainerIsAlwaysBaseCloneScoped:
    """Asserted against the source rather than by launching a repair cycle: the
    launch path is a ~150-line docker argv builder wired to host-path detection
    and a real `docker run`, and what matters here is only that the label is on
    the argv it builds, next to the base-clone mount that justifies it."""

    def test_the_launch_argv_stamps_it_true(self):
        source = Path(__file__).resolve().parents[3].joinpath(
            'services', 'project_monitor.py'
        ).read_text()

        assert re.search(
            rf"'--label',\s*f?'{re.escape(BASE_CLONE_LABEL)}=true'", source
        ), (
            f"project_monitor.py's repair-cycle launch must stamp "
            f"{BASE_CLONE_LABEL}=true -- it mounts the base clone at "
            f"/workspace/<project> regardless of its own project_dir, so the "
            f"startup survivor probe must treat it as able to hold that lock"
        )
