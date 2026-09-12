"""
Dev-container environments (#198).

Several project configs on the same repository can name one environment and
share its image, state file and build lock. The invariant every test here
protects is that opting *out* -- which is every existing deployment -- keys on
exactly the strings it always did, and that opting *in* never lets two members
build the same tag concurrently or redundantly.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.dev_container_environment import (
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

class TestPromptRendering:
    """The tag must reach the RENDERED prompt.

    The tests these replace asserted only on markdown file contents and on
    inspect.getsource() -- they passed with every line of Python in this change
    deleted, while the value never reached the agent at all. Prompt content is
    not otherwise covered, and this is where a wiring mistake is invisible.
    """

    TASK = {
        'project': 'features',
        'issue': {'title': 'T', 'body': 'B', 'labels': []},
        'dev_container_image_tag': 'mono-agent:latest',
        'dev_container_environment': 'mono',
    }

    def _ctx(self, agent, **over):
        from prompts.context import PromptContext
        task = {**self.TASK, **over}
        return PromptContext.from_task_context(
            task,
            agent_name=agent,
            agent_display_name='X',
            agent_role_description='Y',
            output_sections=['Summary'],
        )

    def _builder(self):
        from prompts.builder import PromptBuilder
        return PromptBuilder()

    def test_setup_prompt_carries_the_resolved_tag(self):
        prompt = self._builder().build(self._ctx('dev_environment_setup'))
        assert 'mono-agent:latest' in prompt
        assert '{DEV_CONTAINER_IMAGE_TAG}' not in prompt

    def test_verifier_prompt_carries_the_same_tag(self):
        """Maker and checker must agree. The verifier's prompt composed the tag
        from the project name -- and unlike the setup guidelines, that
        placeholder IS substituted -- so the checker inspected an image the
        maker never built whenever an environment was shared."""
        prompt = self._builder().build_verifier_prompt(self._ctx('dev_environment_verifier'))
        assert 'mono-agent:latest' in prompt
        assert 'features-agent:latest' not in prompt
        assert '{DEV_CONTAINER_IMAGE_TAG}' not in prompt

    def test_unconfigured_deployment_is_unchanged(self):
        """The prompt change is unconditional, so it must be correct for a
        deployment that has opted into nothing."""
        prompt = self._builder().build(self._ctx(
            'dev_environment_setup',
            dev_container_image_tag='features-agent:latest',
            dev_container_environment='features',
        ))
        assert 'features-agent:latest' in prompt

    def test_tag_is_derived_when_not_threaded(self):
        """A context built directly must still render a usable prompt."""
        from unittest.mock import patch
        ctx = self._ctx('dev_environment_setup', dev_container_image_tag='')
        with patch('services.dev_container_environment.image_tag_for',
                   return_value='derived-agent:latest'):
            prompt = self._builder().build(ctx)
        assert 'derived-agent:latest' in prompt

    def test_refuses_to_render_an_untagged_build(self):
        """Substituting an empty string yields `docker build -t  /workspace/x`,
        which builds SOMETHING and fails only much later as 'never built'."""
        ctx = self._ctx('dev_environment_setup', dev_container_image_tag='', project='')
        with pytest.raises(ValueError, match='DEV_CONTAINER_IMAGE_TAG'):
            self._builder().build(ctx)

    def test_setup_prompt_resolves_the_project_path_concretely(self):
        """The path stays project-scoped -- each project keeps its own checkout
        -- and under the unified expansion pass it is now substituted for real
        rather than left as a placeholder for the model to fill in."""
        prompt = self._builder().build(self._ctx('dev_environment_setup'))
        assert '/workspace/features' in prompt
        assert '{PROJECT_NAME}' not in prompt

    def test_path_and_tag_resolve_to_different_identifiers(self):
        """The whole hazard in one assertion: under a shared environment the
        checkout path and the image tag are no longer the same string."""
        prompt = self._builder().build(self._ctx('dev_environment_setup'))
        assert '/workspace/features' in prompt      # project
        assert 'mono-agent:latest' in prompt        # environment


class TestWatchdogActivityKey:
    """The build lock must register watchdog activity under the REAL project.

    The registry is keyed (project, issue_number) and looked up by project name
    by pipeline_watchdog and project_monitor. Registering under the environment
    made every lookup miss for a shared-environment member, so a run
    legitimately blocked on the lock lost its zombie-cleanup exemption and was
    reaped and re-dispatched -- two concurrent executions of one issue.

    Asserts through describe_active_resource_lock_activity(), the same seam the
    watchdog uses. An earlier version of this test grepped the module source;
    it was proven vacuous by mutation -- reintroducing the bug while preserving
    the grepped strings left the whole suite green.
    """

    def test_activity_is_findable_by_project_not_environment(self):
        from services.dev_container_build_lock import dev_container_build_lock_sync
        from services.project_checkout_lock import (
            describe_active_resource_lock_activity,
        )

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, 'acquired')
        facade.release_resource.return_value = True

        configs = {'features': FakeConfig({'environment': 'mono'})}
        with _with_configs(configs):
            with dev_container_build_lock_sync(
                'features', issue_number=42, facade=facade
            ):
                by_project = describe_active_resource_lock_activity('features', 42)
                by_environment = describe_active_resource_lock_activity('mono', 42)

        assert by_project is not None, (
            "the watchdog looks this up by project name; registering under the "
            "environment silently removes the zombie-cleanup exemption")
        assert by_environment is None, (
            "must not be registered under the environment")

    @pytest.mark.asyncio
    async def test_async_variant_registers_under_the_project_too(self):
        """Both variants rebind, so both need covering.

        Mutation testing caught this: reintroducing the bug in the ASYNC entry
        point left the sync-only version of this test green.
        """
        from services.dev_container_build_lock import dev_container_build_lock_async
        from services.project_checkout_lock import (
            describe_active_resource_lock_activity,
        )

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, 'acquired')
        facade.release_resource.return_value = True

        with _with_configs({'features': FakeConfig({'environment': 'mono'})}):
            async with dev_container_build_lock_async(
                'features', issue_number=43, facade=facade
            ):
                by_project = describe_active_resource_lock_activity('features', 43)
                by_environment = describe_active_resource_lock_activity('mono', 43)

        assert by_project is not None
        assert by_environment is None
        assert facade.acquire_resource.call_args[0][0] == 'mono'

    def test_the_lock_itself_still_keys_on_the_environment(self):
        """The other half: the RESOURCE is shared even though the activity
        record is not."""
        from services.dev_container_build_lock import (
            RESOURCE_NAME,
            dev_container_build_lock_sync,
        )

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, 'acquired')
        facade.release_resource.return_value = True

        with _with_configs({'features': FakeConfig({'environment': 'mono'})}):
            with dev_container_build_lock_sync('features', issue_number=42, facade=facade):
                pass

        acquired_key = facade.acquire_resource.call_args[0][0]
        assert acquired_key == 'mono', (
            "two members must contend for one key, or they build the same tag "
            "concurrently")


class TestStartupEnforcement:
    """The fail-closed decision, not just the validator.

    An earlier version grepped main.py's source for the call; replacing
    exit(1) with `pass` left it green, so it could not tell "enforced" from
    "called and ignored" -- the exact distinction it existed to make.
    """

    def test_invalid_config_stops_startup(self):
        from main import _enforce_dev_container_config

        mgr = MagicMock()
        mgr.validate_dev_container_environments.return_value = [
            "Project 'a' and 'b' have different github.repo_url values"
        ]
        logger = MagicMock()
        with pytest.raises(SystemExit) as excinfo:
            _enforce_dev_container_config(mgr, logger)
        assert excinfo.value.code == 1
        # every error must be reported BEFORE the exit, or the operator has
        # nothing to act on
        assert logger.log_error.called
        logged = ' '.join(str(c) for c in logger.log_error.call_args_list)
        assert 'repo_url' in logged
        assert 'Refusing to start' in logged

    def test_valid_config_proceeds_silently(self):
        from main import _enforce_dev_container_config

        mgr = MagicMock()
        mgr.validate_dev_container_environments.return_value = []
        logger = MagicMock()
        _enforce_dev_container_config(mgr, logger)   # must not raise
        assert not logger.log_error.called

    def test_enforcement_precedes_the_first_environment_read(self):
        """Ordering matters: an earlier placement ran after the startup state
        sweep and after setup tasks were pushed into a persistent Redis queue,
        so a restart loop accumulated a fresh set on every attempt."""
        import inspect
        import main

        src = inspect.getsource(main.main)
        enforce = src.index('_enforce_dev_container_config(')
        # the CALL, not the comment several lines above it that names it
        first_read = src.index('workspace_manager.initialize_all_projects')
        assert enforce < first_read


class TestValidatorIsTotal:
    """The validator IS the startup guard, so raising from it crashes the
    process with a traceback naming no project -- the failure it exists to
    report legibly. Every malformed shape must come back as an error string."""

    MALFORMED = [
        ('scalar', 'monorepo'),
        ('list', ['monorepo']),
        ('env-list', {'environment': ['a', 'b']}),
        ('env-int', {'environment': 123}),
        ('env-bool', {'environment': True}),      # unquoted `environment: yes`
    ]

    @pytest.mark.parametrize('label,shape', MALFORMED)
    def test_malformed_shapes_are_reported_not_raised(self, label, shape):
        errors = validate_environments({'features': FakeConfig(shape)})
        assert errors, f"{label} produced no error"
        assert any('features' in e for e in errors), "must name the project"

    @pytest.mark.parametrize('label,shape', MALFORMED)
    def test_resolution_degrades_instead_of_raising(self, label, shape):
        """environment_for sits under every state read, build-lock acquisition
        and tag resolution -- including in processes that never run the
        validator."""
        mgr = MagicMock()
        mgr.get_project_config.return_value = FakeConfig(shape)
        with patch('config.manager.config_manager', mgr):
            assert environment_for('features') == 'features'

    def test_one_malformed_member_does_not_hide_the_others(self):
        """A crash used to abort the whole validation, so the remaining rules
        never ran -- including the cross-repo check."""
        errors = validate_environments({
            'features': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'},
                               repo_url='git@github.com:acme/other.git'),
            'infra': FakeConfig('malformed-scalar'),
        })
        assert any('malformed' in e for e in errors)
        assert any('different github.repo_url' in e for e in errors)


class TestTransientResolutionIsNotCached:
    """A cached wrong answer points a member at its own image for the whole
    TTL, silently bypassing the build lock that serialises the environment."""

    def test_fault_is_not_cached_and_recovers_on_the_next_call(self):
        from unittest.mock import MagicMock, patch
        import services.dev_container_environment as dce

        mgr = MagicMock()
        calls = {'n': 0}

        def flaky(name):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError("transient IO error")
            return FakeConfig({'environment': 'mono'})

        mgr.get_project_config.side_effect = flaky
        with patch('config.manager.config_manager', mgr):
            first = dce.environment_for('features')
            second = dce.environment_for('features')

        assert first == 'features', "must degrade safely on the faulting call"
        assert second == 'mono', "must NOT have cached the degraded answer"

    def test_missing_project_stays_quiet_and_is_cached(self):
        """A name that simply is not a project is routine -- system callers pass
        state-file stems and 'switchyard' constantly."""
        from unittest.mock import MagicMock, patch
        from config.manager import ConfigurationError
        import services.dev_container_environment as dce

        mgr = MagicMock()
        mgr.get_project_config.side_effect = ConfigurationError("no such project")
        with patch('config.manager.config_manager', mgr):
            assert dce.environment_for('not-a-project') == 'not-a-project'


class TestValidationRepoUrlHole:
    def test_member_without_repo_url_is_not_silently_exempt(self):
        """`{r for r in repos.values() if r}` skipped members with no repo_url,
        so the one structural check for a cross-repo share ignored exactly the
        configs that declared nothing."""
        errors = validate_environments({
            'features': FakeConfig({'environment': 'mono'}),
            'bugs': FakeConfig({'environment': 'mono'}, repo_url=None),
        })
        assert any('no github.repo_url' in e for e in errors)
