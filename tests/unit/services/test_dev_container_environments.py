"""
Dev-container environments (#198).

Several project configs on the same repository can name one environment and
share its image, state file and build lock. The invariant every test here
protects is that opting *out* -- which is every existing deployment -- keys on
exactly the strings it always did, and that opting *in* never lets two members
build the same tag concurrently or redundantly.
"""

import copy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

pytest.importorskip("yaml")

from services.dev_container_environment import (  # noqa: E402
    IMAGE_TAG_SUFFIX,
    clear_cache,
    environment_for,
    image_tag_for,
    members_of,
    validate_environments,
)


class FakeConfig:
    """Stand-in for ProjectConfig -- only the two fields this reads."""

    def __init__(self, dev_container=None, repo_url='git@github.com:acme/mono.git'):
        self.dev_container = dev_container
        self.github = {'repo_url': repo_url}


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


def _with_configs(configs):
    """Patch config_manager.get_project_config to serve `configs`."""
    mgr = MagicMock()
    mgr.get_project_config.side_effect = lambda n: configs[n]
    return patch('config.manager.config_manager', mgr)


# ----------------------------------------------------------------- resolution

class TestResolution:
    def test_absent_resolves_to_the_project_itself(self):
        """The property that makes this a no-op for every existing deployment."""
        with _with_configs({'solo': FakeConfig()}):
            assert environment_for('solo') == 'solo'
            assert image_tag_for('solo') == f'solo{IMAGE_TAG_SUFFIX}'

    def test_members_resolve_to_the_shared_environment(self):
        configs = {
            'features': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'}),
        }
        with _with_configs(configs):
            assert environment_for('features') == 'mono'
            assert environment_for('bugs') == 'mono'
            assert image_tag_for('features') == image_tag_for('bugs') == f'mono{IMAGE_TAG_SUFFIX}'

    def test_unknown_project_resolves_to_itself_without_raising(self):
        """System-level callers pass names that aren't projects at all."""
        mgr = MagicMock()
        mgr.get_project_config.side_effect = KeyError('nope')
        with patch('config.manager.config_manager', mgr):
            assert environment_for('not-a-project') == 'not-a-project'

    def test_invalid_name_at_runtime_degrades_to_the_project(self):
        """Validation rejects this at load, so reaching it means a config was
        edited under a running orchestrator. Emitting an unusable Docker tag
        would be worse than ignoring the setting."""
        with _with_configs({'p': FakeConfig({'environment': 'Not A Tag!'})}):
            assert environment_for('p') == 'p'

    def test_members_of(self):
        configs = {
            'features': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'}),
            'solo': FakeConfig(),
        }
        with _with_configs(configs):
            assert members_of('mono', list(configs)) == ['bugs', 'features']
            assert members_of('solo', list(configs)) == ['solo']


# ----------------------------------------------------------------- validation

class TestValidation:
    def test_clean_config_has_no_errors(self):
        assert validate_environments({
            'features': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'}),
            'solo': FakeConfig(repo_url='git@github.com:acme/other.git'),
        }) == []

    def test_members_must_share_a_repository(self):
        """The one structural check that can catch sharing an image across
        codebases -- the image bakes one repo's dependencies."""
        errors = validate_environments({
            'features': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'},
                               repo_url='git@github.com:acme/different.git'),
        })
        assert len(errors) == 1
        assert 'different github.repo_url' in errors[0]

    def test_name_must_be_a_legal_docker_tag_component(self):
        errors = validate_environments({'p': FakeConfig({'environment': 'Not A Tag!'})})
        assert len(errors) == 1
        assert 'not a valid Docker tag component' in errors[0]

    def test_environment_may_not_collide_with_another_projects_name(self):
        """Would quietly make 'solo's implicit environment someone else's
        explicit one, coupling two unrelated projects' images."""
        errors = validate_environments({
            'features': FakeConfig({'environment': 'solo'}),
            'solo': FakeConfig(),
        })
        assert len(errors) == 1
        assert 'also the name of a different project' in errors[0]

    def test_opting_in_under_your_own_name_is_allowed(self):
        """Explicitly naming your own environment is a no-op, not a collision."""
        assert validate_environments({'solo': FakeConfig({'environment': 'solo'})}) == []

    def test_a_shared_name_all_members_opted_into_is_not_a_collision(self):
        assert validate_environments({
            'mono': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'}),
        }) == []


# ------------------------------------------------------- keying (state + lock)

class TestKeying:
    def test_state_file_is_shared_by_members_and_identity_otherwise(self, tmp_path):
        from services.dev_container_state import DevContainerStateManager

        mgr = DevContainerStateManager(state_dir=tmp_path)
        configs = {
            'features': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'}),
            'solo': FakeConfig(),
        }
        with _with_configs(configs):
            assert mgr.get_state_file('features') == mgr.get_state_file('bugs')
            assert mgr.get_state_file('features').name == 'mono.yaml'
            assert mgr.get_state_file('solo').name == 'solo.yaml'

    def test_build_lock_keys_on_the_environment(self):
        """Keying on the project would let two members build the same tag
        concurrently -- exactly the race the lock exists to close (#56)."""
        from services.dev_container_build_lock import _resource_key

        configs = {
            'features': FakeConfig({'environment': 'mono'}),
            'solo': FakeConfig(),
        }
        with _with_configs(configs):
            assert _resource_key('features') == 'mono'
            assert _resource_key('solo') == 'solo'


# ------------------------------------------- Cost 2: double-checked re-read

class TestBuildDeduplication:
    def _probe(self, status_now, status_before):
        from services.dev_container_build_lock import build_already_done_by_another_member
        import services.dev_container_state as dcs

        fake = MagicMock()
        fake.get_status.return_value = status_now
        with patch.object(dcs, 'dev_container_state', fake):
            return build_already_done_by_another_member('bugs', status_before)

    def test_stands_down_when_another_member_built_during_the_wait(self):
        from services.dev_container_state import DevContainerStatus as S
        assert self._probe(S.VERIFIED, S.UNVERIFIED) == S.VERIFIED

    def test_stands_down_when_another_member_failed_during_the_wait(self):
        """A failed build must block the environment once, not once per member."""
        from services.dev_container_state import DevContainerStatus as S
        assert self._probe(S.BLOCKED, S.UNVERIFIED) == S.BLOCKED

    def test_deliberate_rebuild_of_a_verified_environment_is_not_skipped(self):
        """Re-running setup on an already-VERIFIED project is how a Dockerfile
        change gets applied. Skipping it would break that outright."""
        from services.dev_container_state import DevContainerStatus as S
        assert self._probe(S.VERIFIED, S.VERIFIED) is None

    def test_deliberate_rebuild_of_a_blocked_environment_is_not_skipped(self):
        from services.dev_container_state import DevContainerStatus as S
        assert self._probe(S.BLOCKED, S.BLOCKED) is None

    def test_proceeds_when_nobody_finished(self):
        from services.dev_container_state import DevContainerStatus as S
        assert self._probe(S.IN_PROGRESS, S.UNVERIFIED) is None


# ------------------------------------------------- Cost 1: the prompt contract

class TestSetupAgentPrompt:
    """The image tag is emitted by a model following a prompt, not by code.

    Before environments existed the tag and the checkout path were the same
    identifier, so composing `{PROJECT_NAME}-agent:latest` was self-consistent.
    Now they diverge, and a build tagged from the project name would be a
    correct image under a name nothing reads. Prompt content is not otherwise
    test-covered, and this is the one place a prompt edit can silently break a
    keying invariant.
    """

    GUIDELINES = Path('prompts/content/agents/dev_environment_setup/guidelines.md')

    def test_prompt_never_composes_the_tag_from_the_project_name(self):
        text = self.GUIDELINES.read_text()
        assert '{PROJECT_NAME}-agent' not in text, (
            "dev_environment_setup must not derive the image tag from the project "
            "name -- it is the dev-container ENVIRONMENT's tag and the two differ "
            "whenever projects share an environment. Use "
            "{DEV_CONTAINER_IMAGE_TAG}, supplied verbatim in the agent's context."
        )

    def test_prompt_uses_the_supplied_tag_placeholder(self):
        text = self.GUIDELINES.read_text()
        assert '{DEV_CONTAINER_IMAGE_TAG}' in text
        assert 'dev_container_image_tag' in text

    def test_prompt_still_uses_the_project_name_for_paths(self):
        """The path deliberately stays project-scoped: each project keeps its
        own checkout, only the image is shared."""
        text = self.GUIDELINES.read_text()
        assert '/workspace/{PROJECT_NAME}' in text


class TestExecutionContext:
    def test_context_carries_both_identifiers(self):
        """Whatever the prompt references must actually be supplied."""
        import inspect
        import services.agent_executor as ae

        src = inspect.getsource(ae)
        assert "context['dev_container_environment']" in src
        assert "context['dev_container_image_tag']" in src
