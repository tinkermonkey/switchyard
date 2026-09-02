"""
Tests for ProjectConfig.worktree_isolation_enabled (#52's per-project scoping
flag for real epic-worktree isolation).

services/agent_executor.py's EPIC_WORKTREE_SAFE_WORKSPACE_TYPES allowlist
(currently just 'discussions', i.e. the planning_design pipeline) says which
workspace TYPES are safe to isolate; this flag is the separate, per-PROJECT
dimension that actually turns isolation on for one, without affecting every
other project using the same workspace type. Defaults to False so every
existing project keeps today's shared-base-clone behavior unless a chosen
pilot project explicitly opts in.

Uses the real foundations/ directory (so pipeline template lookups resolve
normally) with an isolated tmp projects/ directory, rather than mocking
ConfigManager's internals -- this exercises the real YAML-parsing path.
"""

from config.manager import ConfigManager

MINIMAL_PROJECT_YAML = """
project:
  name: "test-flag-project"
  description: "test project for the worktree_isolation_enabled flag"
  github:
    org: "test-org"
    repo: "test-repo"
  tech_stacks:
    backend: "python"
  pipelines:
    enabled:
      - template: "planning_design"
        name: "Planning"
        board_name: "Planning"
        description: "planning board"
        workflow: "planning_workflow"
        active: true
  pipeline_routing: {{}}
{extra}"""


def _manager_with_project_yaml(tmp_path, extra: str = ""):
    manager = ConfigManager()  # real foundations/ dir, so template lookups work
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    manager.projects_dir = projects_dir
    (projects_dir / "test-flag-project.yaml").write_text(
        MINIMAL_PROJECT_YAML.format(extra=extra)
    )
    return manager


class TestWorktreeIsolationEnabledFlag:
    def test_defaults_to_false_when_absent(self, tmp_path):
        manager = _manager_with_project_yaml(tmp_path)
        config = manager.get_project_config("test-flag-project")
        assert config.worktree_isolation_enabled is False

    def test_true_when_explicitly_enabled(self, tmp_path):
        manager = _manager_with_project_yaml(
            tmp_path, extra="  worktree_isolation_enabled: true\n"
        )
        config = manager.get_project_config("test-flag-project")
        assert config.worktree_isolation_enabled is True

    def test_false_when_explicitly_disabled(self, tmp_path):
        manager = _manager_with_project_yaml(
            tmp_path, extra="  worktree_isolation_enabled: false\n"
        )
        config = manager.get_project_config("test-flag-project")
        assert config.worktree_isolation_enabled is False

    def test_always_reloaded_from_disk_not_cached_stale(self, tmp_path):
        """get_project_config() always re-reads from disk (see its own docstring)
        -- flipping the flag in the YAML and re-fetching must pick up the change,
        not return a stale cached ProjectConfig from before the edit."""
        manager = _manager_with_project_yaml(tmp_path)
        assert manager.get_project_config("test-flag-project").worktree_isolation_enabled is False

        (manager.projects_dir / "test-flag-project.yaml").write_text(
            MINIMAL_PROJECT_YAML.format(extra="  worktree_isolation_enabled: true\n")
        )
        assert manager.get_project_config("test-flag-project").worktree_isolation_enabled is True
