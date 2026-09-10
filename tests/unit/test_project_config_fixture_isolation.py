"""
The test suite must not depend on config/projects/ (#140 items 35 and 38).

config/projects/ is gitignored AS A DIRECTORY and is the REAL deployment's
project config directory. Keeping the suite's fake project configs there made
one file serve two incompatible roles: test input that must exist for the suite
to pass, and live project config the running orchestrator acts on (stale
pipeline locks re-evaluated every startup, board reconciliation, and real
dev_environment_setup / dev_environment_verifier agent runs dispatched against
/workspace/test-project with their output addressed to issue #0 -- see #162).

tests/conftest.py's isolated_project_configs fixture redirects the process-wide
ConfigManager at tests/fixtures/config/projects/ instead. These tests pin that,
so a fresh checkout with an empty config/projects/ passes out of the box and the
fixtures cannot drift back into the deployment's config directory.
"""

from pathlib import Path

import pytest

from config.manager import ConfigurationError, config_manager
from tests.conftest import FIXTURE_PROJECTS_DIR

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PROJECTS = ('test_project', 'test-project')


class TestFixturesLiveInTrackedSource:

    def test_the_fixture_directory_exists(self):
        assert FIXTURE_PROJECTS_DIR.is_dir(), (
            f"{FIXTURE_PROJECTS_DIR} is missing -- the suite's project configs must "
            "live somewhere git actually tracks"
        )

    @pytest.mark.parametrize('project_name', FIXTURE_PROJECTS)
    def test_each_fixture_project_config_is_present(self, project_name):
        assert (FIXTURE_PROJECTS_DIR / f'{project_name}.yaml').is_file()

    def test_the_fixture_directory_is_not_gitignored(self):
        """The whole point: unlike config/projects/, this path is committable.
        Checked by asking git rather than by re-parsing .gitignore."""
        import subprocess

        target = FIXTURE_PROJECTS_DIR / 'test_project.yaml'
        result = subprocess.run(
            ['git', 'check-ignore', '-q', str(target)],
            cwd=REPO_ROOT, capture_output=True,
        )
        # git check-ignore exits 0 when the path IS ignored, 1 when it is not.
        assert result.returncode != 0, f"{target} is gitignored and could not be committed"


class TestConfigManagerReadsTheFixtures:

    @pytest.mark.parametrize('project_name', FIXTURE_PROJECTS)
    def test_fixture_projects_load(self, project_name):
        assert config_manager.get_project_config(project_name).name == project_name

    def test_the_singleton_points_at_the_fixture_directory(self):
        """If this ever reads config/projects/ again, the suite is back to
        depending on files that must not ship."""
        assert config_manager.projects_dir == FIXTURE_PROJECTS_DIR

    def test_fixture_projects_stay_hidden(self):
        """`hidden: true` is what keeps them out of every visible-project loop
        (workspace init, board reconciliation, dispatch). It is load-bearing for
        anyone who does still have a stray copy in a deployment config dir."""
        for project_name in FIXTURE_PROJECTS:
            assert config_manager.get_project_config(project_name).hidden is True

    def test_fixture_projects_enable_no_pipelines(self):
        """A fixture with an enabled pipeline would be dispatchable."""
        for project_name in FIXTURE_PROJECTS:
            assert config_manager.get_project_config(project_name).pipelines == []

    def test_a_project_that_is_neither_fixture_nor_real_still_raises(self):
        """Control: redirection is not a catch-all that invents configs."""
        with pytest.raises(ConfigurationError):
            config_manager.get_project_config('no_such_project_anywhere')


class TestDeploymentConfigDirectoryStaysClean:

    def test_no_fixture_project_config_is_committed_under_config_projects(self):
        """
        The operational half of #140 item 38 cannot be enforced from a branch --
        the stray files in a deployment are untracked, so no commit can delete
        them (that removal is #175). What a branch CAN guarantee is that nothing
        is ever committed to config/projects/ under a fixture name, so the two
        roles cannot be conflated again on purpose.
        """
        import subprocess

        result = subprocess.run(
            ['git', 'ls-files', 'config/projects/'],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.stdout.strip() == '', (
            f"config/projects/ has tracked files: {result.stdout.strip()}"
        )
