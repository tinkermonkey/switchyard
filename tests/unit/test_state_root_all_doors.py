"""Every module that defaults a state directory goes through one resolver (#202).

#200 closed two doors. Seven more were still open: each of these modules
open-coded

    orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
    state_dir = Path(orchestrator_root) / "state" / "<subdir>"
    state_dir.mkdir(parents=True, exist_ok=True)   # a line or two later

with no strip, no resolve and no validation. `os.environ.get(key, '/app')`
returns `''` when the key EXISTS BUT IS EMPTY -- `-e ORCHESTRATOR_ROOT=` on a
docker run -- so the `/app` default never applied to that case, and
`Path('') / "state"` is a CWD-relative `state`, mkdir'd wherever the process
happened to be standing.

The tests below are behavioural: they construct the real managers and look at
the directory each one chose. Nothing here greps source text, because the
thing being protected is where a mkdir lands, and a grep cannot see that.

Nothing here creates a directory outside tmp_path either. Where an assertion
concerns a root that would resolve OUTSIDE tmp_path (the empty-string case
resolves to the checkout's own `state/`), Path.mkdir is stubbed for the
duration of the construction -- the point of that case is the path that was
chosen, not that it was created.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# tests/unit/<this file> -> the checkout root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# (label, subdir under state/, factory taking an optional state_dir)
#
# Each factory passes whatever that constructor needs to stay off the network:
# PipelineLockManager(use_redis=False) skips its Redis connect entirely, and
# PipelineSemaphoreManager takes an injected client for the same reason.
def _work_execution(state_dir=None):
    from services.work_execution_state import WorkExecutionStateTracker
    return WorkExecutionStateTracker(state_dir=state_dir)


def _dev_container(state_dir=None):
    from services.dev_container_state import DevContainerStateManager
    return DevContainerStateManager(state_dir=state_dir)


def _pipeline_lock(state_dir=None):
    from services.pipeline_lock_manager import PipelineLockManager
    return PipelineLockManager(state_dir=state_dir, use_redis=False)


def _pipeline_queue(state_dir=None):
    from services.pipeline_queue_manager import PipelineQueueManager
    return PipelineQueueManager('a-project', 'a-board', state_dir)


def _pipeline_semaphore(state_dir=None):
    from services.pipeline_semaphore_manager import PipelineSemaphoreManager
    return PipelineSemaphoreManager(state_dir=state_dir, redis_client=MagicMock())


def _conversational_session(state_dir=None):
    from services.conversational_session_state import ConversationalSessionStateManager
    return ConversationalSessionStateManager(state_dir=state_dir)


THE_SEVEN = [
    ('work_execution_state', 'execution_history', _work_execution),
    ('dev_container_state', 'dev_containers', _dev_container),
    ('pipeline_lock_manager', 'pipeline_locks', _pipeline_lock),
    ('pipeline_queue_manager', 'pipeline_queues', _pipeline_queue),
    ('pipeline_semaphore_manager', 'pipeline_semaphores', _pipeline_semaphore),
    ('conversational_session_state', 'conversational_sessions', _conversational_session),
]

CASES = pytest.mark.parametrize(
    'label,subdir,build',
    THE_SEVEN,
    ids=[row[0] for row in THE_SEVEN],
)


@CASES
def test_the_default_state_dir_comes_from_the_one_resolver(
    label, subdir, build, tmp_path
):
    """Patching orchestrator_state_root() moves the manager's directory.

    This is the invariant, stated the only way that cannot be faked: if the
    module still read ORCHESTRATOR_ROOT itself, the patch would not reach it
    and it would land in conftest's scratch root instead of tmp_path.
    """
    fake_root = tmp_path / 'state'

    with patch('config.state_manager.orchestrator_state_root', return_value=fake_root):
        manager = build()

    assert manager.state_dir == fake_root / subdir
    assert manager.state_dir.is_dir(), f"{label} did not create its state dir"


@CASES
def test_an_empty_orchestrator_root_is_not_resolved_against_the_cwd(
    label, subdir, build, monkeypatch, tmp_path
):
    """`-e ORCHESTRATOR_ROOT=` is the accident this issue is named for.

    The key exists and is empty, so `os.environ.get('ORCHESTRATOR_ROOT',
    '/app')` returned `''`, not `/app`, and `Path('') / "state" / <subdir>` is
    the relative `state/<subdir>` -- created under whatever the CWD is. The
    resolver reads empty as unset and falls back to the checkout's own state
    tree, which is absolute.

    mkdir is stubbed: the correct answer here lies outside tmp_path (it is the
    checkout), and this test must not create it.
    """
    monkeypatch.setenv('ORCHESTRATOR_ROOT', '')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, 'mkdir', lambda self, **kwargs: None)

    manager = build()

    assert manager.state_dir.is_absolute(), (
        f"{label} chose {manager.state_dir} -- a relative state dir, which "
        f"mkdir would have created under {tmp_path}"
    )
    assert tmp_path not in manager.state_dir.parents
    assert manager.state_dir.name == subdir


@CASES
def test_a_whitespace_orchestrator_root_is_not_a_directory_named_space(
    label, subdir, build, monkeypatch, tmp_path
):
    """`Path('   ') / "state"` is a real, creatable directory called `   `."""
    monkeypatch.setenv('ORCHESTRATOR_ROOT', '   ')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, 'mkdir', lambda self, **kwargs: None)

    manager = build()

    assert manager.state_dir.is_absolute()
    assert '   ' not in manager.state_dir.parts


@CASES
def test_a_relative_orchestrator_root_is_refused_rather_than_created(
    label, subdir, build, monkeypatch, tmp_path
):
    """A dropped leading slash used to mean "write under the CWD".

    chdir into tmp_path first so that a regression -- which would mkdir
    `relative-oops/state/<subdir>` instead of raising -- leaves its mess in a
    directory pytest deletes rather than in the checkout. The assertion on
    tmp_path afterwards is what makes that visible instead of merely harmless.
    """
    monkeypatch.setenv('ORCHESTRATOR_ROOT', 'relative-oops')
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match='absolute'):
        build()

    assert not (tmp_path / 'relative-oops').exists()


@CASES
def test_an_explicit_state_dir_still_wins(label, subdir, build, tmp_path):
    """The resolver is the DEFAULT, not an override.

    Every one of these constructors takes a state_dir, and the test suite and
    scripts/ pass one constantly. A fix that started ignoring it would break
    far more than it fixed, so pin it.
    """
    chosen = tmp_path / 'chosen'

    manager = build(state_dir=chosen)

    assert manager.state_dir == chosen
    assert chosen.is_dir()


@CASES
def test_a_real_orchestrator_root_lands_under_it(
    label, subdir, build, monkeypatch, tmp_path
):
    """End to end with no patching at all: env in, directory on disk out."""
    monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))

    manager = build()

    assert manager.state_dir == tmp_path / 'state' / subdir
    assert manager.state_dir.is_dir()


class TestTheSeventhDoorIsNotAConstructor:
    """services/scheduled_tasks.py open-coded the SAME pipeline_queues path
    that PipelineQueueManager's own default resolves, and passed it in.

    Two copies of one resolution is how they drift; the copy here was the
    unvalidated one. The fix is to pass no state_dir at all, so this asserts on
    the call rather than on a directory -- the directory is already covered by
    the PipelineQueueManager cases above.
    """

    @pytest.mark.asyncio
    async def test_reconcile_queue_state_lets_the_queue_manager_resolve_its_own_dir(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))

        from services.scheduled_tasks import ScheduledTasksService

        project_config = MagicMock()
        project_config.pipelines = [MagicMock(board_name='dev_workflow')]
        config_manager = MagicMock()
        config_manager.list_visible_projects.return_value = ['a-project']
        config_manager.get_project_config.return_value = project_config

        service = ScheduledTasksService()

        with patch('config.manager.config_manager', config_manager), \
                patch(
                    'services.pipeline_queue_manager.PipelineQueueManager'
                ) as queue_manager_class, \
                patch.object(
                    ScheduledTasksService, '_reset_stranded_active_issues', return_value=0
                ):
            await service._reconcile_queue_state()

        queue_manager_class.assert_called_once_with('a-project', 'dev_workflow')

    def test_scheduled_tasks_no_longer_computes_a_state_path_of_its_own(self):
        """The module reads ORCHESTRATOR_ROOT nowhere.

        A companion to the call assertion above, which would still pass if the
        open-coded path were computed and then thrown away -- and a computed
        path in this module has a habit of acquiring a mkdir.
        """
        import services.scheduled_tasks as scheduled_tasks

        source = Path(scheduled_tasks.__file__).read_text()
        offenders = [
            line.strip()
            for line in source.splitlines()
            if 'ORCHESTRATOR_ROOT' in line and not line.strip().startswith('#')
        ]

        assert offenders == [], (
            "services/scheduled_tasks.py reads ORCHESTRATOR_ROOT again; state "
            "paths belong to config.state_manager.orchestrator_state_root() "
            "(#202):\n  " + "\n  ".join(offenders)
        )


class TestNothingButTheResolverReadsARootFromTheEnvironment:
    """Repository-wide, because the first pass of #202 was not.

    The tripwire this replaces importlib-imported `services.<the seven>` and
    looked at those seven files, while config/state_manager.py's docstring
    claimed to be "the ONLY place that reads it to build a state path". Two
    counterexamples were live at the time and invisible to that guard:
    services/data_retention.py open-coded both roots (and a sweep DELETES from
    what it returns), and six scripts/ entry points open-coded
    `Path(os.environ.get('ORCHESTRATOR_ROOT', <default>)) / 'state' / ...`
    followed by a mkdir -- #202's headline bug verbatim, in the tree the
    issue's own "why it has not bitten" section names as unprotected.

    Walking the repository is the only scope that matches the claim being made.
    A per-module list is a guard that can only see the doors someone already
    remembered.

    It is still a text match, and still narrow on purpose: the exact
    co-occurrence of `environ.get(` and a quoted root name on one line. It does
    not see `os.environ['ORCHESTRATOR_ROOT']` -- scripts/dry_run_state_sweep.py
    legitimately WRITES the variable that way, and no text pattern separates
    that from a read. The behavioural tests in this file are the real guard;
    this only catches the shape coming back, which in this repo it has.
    """

    # path -> why it legitimately reads one
    EXEMPT = {
        # It is the resolver. Its actual read is `os.environ.get(name)` with
        # the root name as a PARAMETER, which the pattern below cannot see
        # anyway; the one line that does match is in the module docstring,
        # quoting the bug. Exempted explicitly rather than relying on that,
        # because the day someone inlines a literal here is not the day this
        # guard should start failing.
        'config/paths.py': 'defines the resolver',
    }

    SKIP_TREES = ('tests', '.claude', '.git', 'node_modules', 'venv', '.venv')

    def test_no_module_outside_config_paths_resolves_a_root_itself(self):
        import re

        pattern = re.compile(
            r"""environ\.get\(['"](?:ORCHESTRATOR_ROOT|WORKSPACE_ROOT)['"]"""
        )

        offenders = []
        for source in sorted(_REPO_ROOT.rglob('*.py')):
            relative = source.relative_to(_REPO_ROOT)
            if relative.parts[0] in self.SKIP_TREES:
                continue
            if str(relative) in self.EXEMPT:
                continue
            for number, line in enumerate(
                source.read_text(errors='ignore').splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if pattern.search(line):
                    offenders.append(f"{relative}:{number}: {stripped[:100]}")

        assert offenders == [], (
            "these lines resolve a root from the environment by hand. "
            "`os.environ.get(name, <default>)` returns '' when the key exists "
            "and is empty (`-e ORCHESTRATOR_ROOT=` on a docker run), so the "
            "default never applies to the case that actually happens, and "
            "Path('') is the CWD (#202). Use config.paths.orchestrator_root(), "
            "orchestrator_state_root() or workspace_root():\n  "
            + "\n  ".join(offenders)
        )


class TestTheDataRetentionDoor:
    """resolve_roots() read both roots raw -- and sweep() DELETES from them.

    This was the second place config/state_manager.py's "ONLY place" docstring
    was wrong about, and the more serious of the two: _PROTECTED_ROOTS lists
    only /app, /workspace and /, so a root that is merely RELATIVE sails past
    it. Measured on this branch before the fix:
    `ORCHESTRATOR_ROOT='   '` gave
    `{'orchestrator': PosixPath('   '), 'workspace': PosixPath('   ')}`.
    """

    def test_a_whitespace_root_is_no_longer_a_relative_sweep_root(
        self, monkeypatch
    ):
        from services.data_retention import resolve_roots

        monkeypatch.setenv('ORCHESTRATOR_ROOT', '   ')
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)

        roots = resolve_roots()

        assert [p for p in roots.values() if not p.is_absolute()] == [], (
            f"a sweep would recurse from a relative root: {roots}"
        )
        assert roots == {
            'orchestrator': Path('/app'),
            'workspace': Path('/workspace'),
        }

    @pytest.mark.parametrize('name', ['ORCHESTRATOR_ROOT', 'WORKSPACE_ROOT'])
    def test_a_relative_root_is_refused_rather_than_swept(
        self, monkeypatch, name
    ):
        """A dropped leading slash is the likeliest typo in the documented
        command, and here it decides what gets deleted."""
        from services.data_retention import resolve_roots

        monkeypatch.setenv(name, 'relative-oops')

        with pytest.raises(ValueError, match='absolute'):
            resolve_roots()

    def test_an_absolute_override_still_carries_the_workspace_rules_with_it(
        self, monkeypatch, tmp_path
    ):
        """The behaviour resolve_roots() exists for, unchanged by the
        validation: a scratch orchestrator root moves the workspace-rooted
        rules under it too, rather than reaching out to the real /workspace."""
        from services.data_retention import resolve_roots

        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)

        assert resolve_roots() == {
            'orchestrator': tmp_path.resolve(),
            'workspace': tmp_path.resolve(),
        }

    def test_the_unset_default_stays_the_literal_app(self, monkeypatch):
        """Deliberately NOT config.paths.orchestrator_root().

        The two are the same directory on the deployment and different ones in
        a worktree or a developer checkout -- and _PROTECTED_ROOTS is keyed on
        the literal '/app'. Resolving the unset case to "whatever checkout this
        code was imported from" would have handed the pytest guard an absolute,
        plausible, unlisted path, so the check that exists to shout when a test
        sweeps production would have gone quiet instead. A fix that turns a
        loud failure into a silent one is not a fix.
        """
        from services.data_retention import (
            _refuse_unisolated_apply,
            resolve_roots,
        )

        monkeypatch.delenv('ORCHESTRATOR_ROOT', raising=False)
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)

        roots = resolve_roots()
        assert roots == {
            'orchestrator': Path('/app'),
            'workspace': Path('/workspace'),
        }

        with pytest.raises(RuntimeError, match='Refusing to apply retention'):
            _refuse_unisolated_apply(roots)


# The scripts/ entry points that resolve a root at import time, and so can be
# observed by importing them. generate_strategy is deliberately absent: it
# resolves inside the two functions that need it, so there is nothing to see at
# import. The repository-wide tripwire above is what covers that file.
SCRIPTS_THAT_RESOLVE_A_ROOT_AT_IMPORT = [
    'analyze_codebase',
    'generate_artifacts',
    'validate_artifacts',
    'maintain_agent_team',
    'rebuild_project_images',
]

# Of those, the four that also BIND the resolved root to a module-scope
# constant, so the value itself can be read back. rebuild_project_images is
# absent: it presets ORCHESTRATOR_ROOT into the environment for config_manager
# and resolves inside get_workspace_root(), so it has no such constant. Its
# refusal comes from services.dev_container_state, which it imports -- which is
# why it is in the list above and not this one.
SCRIPTS_THAT_BIND_A_ROOT_AT_IMPORT = [
    'analyze_codebase',
    'generate_artifacts',
    'validate_artifacts',
    'maintain_agent_team',
]


def _import_script_in_a_subprocess(module: str, cwd: Path, root: str):
    environment = dict(os.environ)
    environment['ORCHESTRATOR_ROOT'] = root
    environment['PYTHONPATH'] = str(_REPO_ROOT)
    return subprocess.run(
        [
            sys.executable,
            '-c',
            f'import scripts.{module} as m; '
            f'print("BOUND", getattr(m, "ORCHESTRATOR_ROOT", "<none>"))',
        ],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestTheScriptsDoors:
    """#202's headline bug, verbatim, in files the first pass did not touch.

    `ORCHESTRATOR_ROOT = Path(os.environ.get('ORCHESTRATOR_ROOT', '.'))` then
    `output_dir = ORCHESTRATOR_ROOT / 'state' / 'projects' / project /
    'analysis'; output_dir.mkdir(parents=True, exist_ok=True)`. Measured before
    the fix with `-e ORCHESTRATOR_ROOT=`: analyze_codebase, validate_artifacts,
    generate_artifacts and maintain_agent_team all bound PosixPath('.'), so
    that mkdir landed under whatever the CWD was -- which for the documented
    invocation is the checkout, i.e. the deployment.

    A SUBPROCESS, not importlib.reload: the value under test only exists at
    module scope, and reloading these inside the session would rebind
    services.dev_container_state's process-wide singleton (which
    rebuild_project_images imports) for every test that runs afterwards.

    The probe root is RELATIVE rather than empty on purpose. Both are the bug,
    but only the relative one can be observed without creating anything: an
    empty root resolves to the checkout's own `state/`, and importing
    rebuild_project_images mkdirs it.
    """

    @pytest.mark.parametrize('module', SCRIPTS_THAT_RESOLVE_A_ROOT_AT_IMPORT)
    def test_a_relative_root_is_refused_at_import_rather_than_bound(
        self, module, tmp_path
    ):
        done = _import_script_in_a_subprocess(
            module, cwd=tmp_path, root='relative-oops'
        )

        assert done.returncode != 0, (
            f"scripts/{module}.py imported cleanly with "
            f"ORCHESTRATOR_ROOT='relative-oops' and bound "
            f"{done.stdout.strip()!r}. Every `root / 'state' / ...` in it is "
            f"then a mkdir under whatever the CWD happens to be (#202)"
        )
        assert 'must be an absolute path' in done.stderr, (
            f"scripts/{module}.py failed for some reason other than the root "
            f"resolver, so this test is no longer measuring what it "
            f"claims:\n{done.stderr[-2000:]}"
        )
        assert list(tmp_path.iterdir()) == [], (
            f"scripts/{module}.py created something under the CWD on its way "
            f"to refusing the root: {list(tmp_path.iterdir())}"
        )

    @pytest.mark.parametrize('module', SCRIPTS_THAT_RESOLVE_A_ROOT_AT_IMPORT)
    def test_an_absolute_root_is_honoured(self, module, tmp_path):
        """The other half: refusing everything would also pass the test above.

        Anything these modules create on import lands under tmp_path, which is
        the point of pointing them there.
        """
        scratch = tmp_path / 'scratch'
        scratch.mkdir()

        done = _import_script_in_a_subprocess(
            module, cwd=tmp_path, root=str(scratch)
        )

        assert done.returncode == 0, done.stderr[-2000:]
        if module in SCRIPTS_THAT_BIND_A_ROOT_AT_IMPORT:
            assert done.stdout.strip() == f'BOUND {scratch}', done.stdout


class TestTheObservabilityServersDoor:
    """It had the same hole in a different shape: a lazy import of
    config.state_manager on a request path, in a container that never built
    that module's singleton, wrapped in `except Exception: return {}`.
    """

    def test_the_state_manager_import_is_at_module_scope(self):
        """So a bad root kills the server at boot, as it does the orchestrator.

        main.py:16 imports config.state_manager and therefore dies on a bad
        ORCHESTRATOR_ROOT before anything runs. The observability server had no
        such import -- its only one was inside _load_github_state() -- so it
        started happily and failed per-request, silently.
        """
        from services import observability_server

        assert hasattr(observability_server, 'orchestrator_state_root'), (
            "services/observability_server.py no longer imports "
            "orchestrator_state_root at module scope; a misconfigured "
            "ORCHESTRATOR_ROOT is back to being a per-request failure (#202)"
        )

    def test_an_unreadable_state_file_is_logged_rather_than_swallowed(
        self, monkeypatch, tmp_path, caplog
    ):
        """`except Exception: return {}` with no log made three different
        situations identical to the Web UI: no state file, a corrupt one, and a
        configuration error. Only the first is normal."""
        import logging

        from services import observability_server

        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        project_dir = tmp_path / 'state' / 'projects' / 'a-project'
        project_dir.mkdir(parents=True)
        # Unterminated flow sequence: yaml.safe_load raises ScannerError.
        (project_dir / 'github_state.yaml').write_text('github_state: {boards: [\n')

        with caplog.at_level(logging.WARNING):
            result = observability_server._load_github_state('a-project')

        assert result == {}
        assert any(
            'a-project' in record.message and 'github_state.yaml' in record.message
            for record in caplog.records
        ), f"nothing logged; records={[r.message for r in caplog.records]}"

    def test_a_project_with_no_state_file_is_still_silent(
        self, monkeypatch, tmp_path, caplog
    ):
        """The other half of the same change: a project that has simply not
        been reconciled yet is NOT an error, and turning that into a warning on
        every request would be the opposite mistake."""
        import logging

        from services import observability_server

        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))

        with caplog.at_level(logging.WARNING):
            result = observability_server._load_github_state('never-reconciled')

        assert result == {}
        assert [r for r in caplog.records if 'never-reconciled' in r.message] == []

    def test_a_configuration_error_is_not_swallowed(self, monkeypatch):
        """A relative ORCHESTRATOR_ROOT reaching this function is a
        configuration error, and it used to come back as an empty dict -- i.e.
        as "this project has no board and no repo"."""
        from services import observability_server

        monkeypatch.setenv('ORCHESTRATOR_ROOT', 'relative-oops')

        with pytest.raises(ValueError, match='absolute'):
            observability_server._load_github_state('a-project')
