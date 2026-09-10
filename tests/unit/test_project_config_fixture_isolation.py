"""
The test suite must not depend on config/projects/ (#140 items 35 and 38).

config/projects/ is gitignored AS A DIRECTORY and is the REAL deployment's
project config directory. Keeping the suite's fake project configs there made
one file serve two incompatible roles: test input that must exist for the suite
to pass, and live project config the running orchestrator acts on (stale
pipeline locks re-evaluated every startup, board reconciliation, and real
dev_environment_setup / dev_environment_verifier agent runs dispatched against
/workspace/test-project with their output addressed to issue #0 -- see #162).

tests/conftest.py's isolated_project_configs fixture points the process-wide
ConfigManager at a session OVERLAY of tests/fixtures/config/projects/ over
config/projects/ instead. These tests pin that, so a fresh checkout with an
empty config/projects/ passes out of the box, the fixtures cannot drift back
into the deployment's config directory, and -- #154/WI-9 review -- a test that
legitimately asks for a real deployment project by name still gets it rather
than a FileNotFoundError pointing into tests/fixtures/.
"""

from pathlib import Path

import pytest

from config.manager import ConfigurationError, config_manager
from tests.conftest import FIXTURE_PROJECTS_DIR, build_project_config_overlay

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

    def test_the_singleton_does_not_read_the_deployment_directory_directly(self):
        """If this ever points straight at config/projects/ again, the suite is
        back to depending on files that must not ship."""
        deployment_dir = REPO_ROOT / 'config' / 'projects'
        assert config_manager.projects_dir != deployment_dir

    @pytest.mark.parametrize('project_name', FIXTURE_PROJECTS)
    def test_each_fixture_resolves_through_to_the_tracked_fixture_file(self, project_name):
        """The overlay must not be a copy that can drift: each entry resolves
        back to the tracked file under tests/fixtures/."""
        entry = config_manager.projects_dir / f'{project_name}.yaml'
        assert entry.resolve() == (FIXTURE_PROJECTS_DIR / f'{project_name}.yaml').resolve()

    def test_a_real_deployment_project_config_is_still_reachable(self):
        """
        THE #154/WI-9 regression. isolated_project_configs is autouse and
        session-scoped, so it applies to every test in the suite -- including
        ones that legitimately ask ConfigManager for a real deployment project
        by name (tests/integration/test_readonly_filesystem.py does, for
        'context-studio'). Replacing projects_dir outright made all of those
        fail with a FileNotFoundError naming a path under tests/fixtures/, with
        nothing to point at the conftest fixture that moved the directory.

        Skips rather than fails on a checkout whose config/projects/ is empty
        (a fresh clone, or a git worktree -- the directory is gitignored, so a
        worktree gets none of it): there is no real project to reach for, and
        that emptiness is exactly what the fixture directory exists to make
        survivable.
        """
        deployment_dir = REPO_ROOT / 'config' / 'projects'
        real = [
            path for path in sorted(deployment_dir.glob('*.yaml'))
            if path.stem not in FIXTURE_PROJECTS
        ]
        if not real:
            pytest.skip(f"no real project configs in {deployment_dir} to reach for")

        assert config_manager.get_project_config(real[0].stem).name is not None

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


class TestTheOverlayIsAdditive:
    """
    build_project_config_overlay() in isolation (#154/WI-9 review) -- the same
    contract TestConfigManagerReadsTheFixtures pins through the singleton, but
    with both source directories under the test's control, so it holds on a
    checkout whose config/projects/ is empty too.
    """

    @staticmethod
    def _write(directory: Path, name: str, body: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body)
        return path

    def test_both_sources_are_present_in_the_overlay(self, tmp_path):
        fixtures = tmp_path / 'fixtures'
        deployment = tmp_path / 'deployment'
        self._write(fixtures, 'test_project.yaml', 'fixture')
        self._write(deployment, 'context-studio.yaml', 'real')

        overlay = build_project_config_overlay(tmp_path / 'overlay', fixtures, deployment)

        assert (overlay / 'test_project.yaml').read_text() == 'fixture'
        assert (overlay / 'context-studio.yaml').read_text() == 'real'

    def test_a_fixture_shadows_a_same_named_deployment_config(self, tmp_path):
        """A stray config/projects/test_project.yaml left over in a deployment
        (#162) must not be what the suite reads."""
        fixtures = tmp_path / 'fixtures'
        deployment = tmp_path / 'deployment'
        self._write(fixtures, 'test_project.yaml', 'fixture')
        self._write(deployment, 'test_project.yaml', 'stray deployment copy')

        overlay = build_project_config_overlay(tmp_path / 'overlay', fixtures, deployment)

        assert (overlay / 'test_project.yaml').read_text() == 'fixture'

    def test_an_absent_deployment_directory_is_not_an_error(self, tmp_path):
        """A fresh clone or a git worktree has no config/projects/ at all."""
        fixtures = tmp_path / 'fixtures'
        self._write(fixtures, 'test_project.yaml', 'fixture')

        overlay = build_project_config_overlay(
            tmp_path / 'overlay', fixtures, tmp_path / 'does-not-exist'
        )

        assert [p.name for p in overlay.glob('*.yaml')] == ['test_project.yaml']

    def test_entries_are_links_not_copies(self, tmp_path):
        """A copy would let the overlay drift from the tracked fixture it stands
        in for, silently, for the whole session."""
        fixtures = tmp_path / 'fixtures'
        source = self._write(fixtures, 'test_project.yaml', 'fixture')

        overlay = build_project_config_overlay(
            tmp_path / 'overlay', fixtures, tmp_path / 'deployment'
        )

        assert (overlay / 'test_project.yaml').resolve() == source.resolve()
