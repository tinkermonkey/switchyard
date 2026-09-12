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
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# tests/unit/<this file> -> the checkout root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# IMPORTED HERE, AT MODULE SCOPE, AND NOT INSIDE THE FACTORIES BELOW.
#
# Three of these modules end with a module-scope singleton --
# `work_execution_tracker = WorkExecutionStateTracker()`,
# `dev_container_state = DevContainerStateManager()`,
# `conversational_session_state = ConversationalSessionStateManager()` -- so the
# FIRST import of one of them anywhere in the pytest session permanently binds
# that process-wide object's state_dir. The factories used to do those imports
# lazily, which meant the empty- and whitespace-root tests below (which
# deliberately poison ORCHESTRATOR_ROOT so the resolver falls back to the
# checkout) were the things triggering the first import. Measured on this branch
# before the change, with a pytest_sessionfinish probe:
# `pytest tests/unit/test_state_root_all_doors.py -k empty_orchestrator_root`
# ended with all three singletons bound to `<checkout>/state/{execution_history,
# dev_containers,conversational_sessions}` -- and on the deployment the checkout
# IS /app, so that is #181 reintroduced by the tests written to prevent it.
# A full `pytest tests/unit` escaped only because tests/unit/services/ is
# collected first and imports two of these under conftest's scratch root; the
# isolation rested on collection order, not on anything enforced.
#
# conftest's import-time root guard cannot help: the binding happens at runtime,
# long after the guard ran. `monkeypatch.setattr(Path, 'mkdir', ...)` cannot
# either: it stops the directory being CREATED at that instant, not the singleton
# pointing at it for the rest of the session.
#
# Collection imports this module after tests/conftest.py has already redirected
# ORCHESTRATOR_ROOT to scratch, so binding the singletons here binds them to
# scratch. test_the_singletons_are_bound_before_any_test_poisons_the_root below
# holds that true.
from services.conversational_session_state import ConversationalSessionStateManager
from services.dev_container_state import DevContainerStateManager
from services.pipeline_lock_manager import PipelineLockManager
from services.pipeline_queue_manager import PipelineQueueManager
from services.pipeline_semaphore_manager import PipelineSemaphoreManager
from services.work_execution_state import WorkExecutionStateTracker


# (label, subdir under state/, factory taking an optional state_dir)
#
# Each factory passes whatever that constructor needs to stay off the network:
# PipelineLockManager(use_redis=False) skips its Redis connect entirely, and
# PipelineSemaphoreManager takes an injected client for the same reason.
def _work_execution(state_dir=None):
    return WorkExecutionStateTracker(state_dir=state_dir)


def _dev_container(state_dir=None):
    return DevContainerStateManager(state_dir=state_dir)


def _pipeline_lock(state_dir=None):
    return PipelineLockManager(state_dir=state_dir, use_redis=False)


def _pipeline_queue(state_dir=None):
    return PipelineQueueManager('a-project', 'a-board', state_dir)


def _pipeline_semaphore(state_dir=None):
    return PipelineSemaphoreManager(state_dir=state_dir, redis_client=MagicMock())


def _conversational_session(state_dir=None):
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


# module path -> the name of the process-wide singleton it builds at import.
THE_SINGLETONS = {
    'services.work_execution_state': 'work_execution_tracker',
    'services.dev_container_state': 'dev_container_state',
    'services.conversational_session_state': 'conversational_session_state',
}

_NESTED = 'SWITCHYARD_ALL_DOORS_NESTED'


class TestTheTestsDoNotReopenTheDoorTheyGuard:
    """The poisoning tests above must not be what first imports the singletons.

    Run in a SUBPROCESS, selecting only the two tests that poison
    ORCHESTRATOR_ROOT, so the answer does not depend on what else the session
    happened to collect first. An in-process assertion cannot say this: whether
    it catches the regression depends on whether it runs before or after the
    poisoning test, and on whether tests/unit/services/ was collected first --
    which is exactly the accident this file's isolation used to rest on.
    """

    _PROBE = textwrap.dedent(
        '''
        import sys
        import pytest

        SINGLETONS = {singletons!r}

        class Probe:
            def pytest_sessionfinish(self, session, exitstatus):
                for module, attribute in SINGLETONS.items():
                    loaded = sys.modules.get(module)
                    where = (
                        getattr(loaded, attribute).state_dir
                        if loaded is not None else "<not imported>"
                    )
                    # Leading newline: pytest's own progress line has no
                    # trailing one under --capture=no (set in pytest.ini), so
                    # the first PROBE would otherwise share a line with it and
                    # the parser below would miss exactly one singleton.
                    print("\\nPROBE", module, where)

        sys.exit(pytest.main({argv!r}, plugins=[Probe()]))
        '''
    )

    def test_the_singletons_are_bound_before_any_test_poisons_the_root(self):
        if os.environ.get(_NESTED):
            pytest.skip('this is the nested run; it must not recurse')

        scratch_root = os.environ.get('ORCHESTRATOR_ROOT')
        assert scratch_root, "conftest must have redirected ORCHESTRATOR_ROOT"

        # Neither name appears in this test's own id, so the nested run cannot
        # select it; the env marker above is the belt to that's braces.
        argv = [
            str(Path(__file__).relative_to(_REPO_ROOT)),
            '-k', 'empty_orchestrator_root or whitespace_orchestrator_root',
            '-p', 'no:randomly',
            '-q',
        ]
        environment = dict(os.environ)
        environment[_NESTED] = '1'
        environment['PYTHONPATH'] = str(_REPO_ROOT)

        done = subprocess.run(
            [
                sys.executable, '-c',
                self._PROBE.format(singletons=THE_SINGLETONS, argv=argv),
            ],
            cwd=str(_REPO_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert done.returncode == 0, (
            f"the nested selection did not pass, so its probe says nothing "
            f"about where the singletons landed:\n{done.stdout[-3000:]}"
            f"\n{done.stderr[-3000:]}"
        )

        probed = dict(
            line.split()[1:3]
            for line in done.stdout.splitlines()
            if line.startswith('PROBE ')
        )
        assert set(probed) == set(THE_SINGLETONS), (
            f"probe did not report every singleton: {probed}"
        )

        for module, where in probed.items():
            assert where != '<not imported>', (
                f"{module} was never imported, so this test proved nothing"
            )
            chosen = Path(where)
            assert _REPO_ROOT not in chosen.parents, (
                f"{module}'s process-wide singleton bound {chosen}, inside the "
                f"checkout. On the deployment the checkout is /app, so every "
                f"later write through that object lands in production state "
                f"(#181). Import the module at test-module scope, before any "
                f"test manipulates ORCHESTRATOR_ROOT."
            )
            assert Path(scratch_root) in chosen.parents, (
                f"{module}'s singleton bound {chosen}, which is not under the "
                f"session's scratch root {scratch_root}"
            )


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

    APP_ROOT is in the alternation because it was the last live instance of the
    bug class: mcp/server.py had `Path(os.environ.get("APP_ROOT", "/app"))` and
    hung WORKFLOWS_YAML, PROJECTS_CONFIG_DIR and STATE_DIR off it, which the
    ORCHESTRATOR_ROOT|WORKSPACE_ROOT pattern could not see at all. It is a
    read-only path, so a blank APP_ROOT degraded to "the MCP tools report no
    board state" rather than to a write -- the same bug, a smaller crater.
    """

    # path -> why it legitimately reads one. Checked for LIVENESS below: an
    # entry whose file has stopped matching is a blind spot with no remaining
    # reason, and it is always the file most worth watching that acquires one.
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
            r"""environ\.get\(['"](?:ORCHESTRATOR_ROOT|WORKSPACE_ROOT|APP_ROOT)['"]"""
        )

        offenders = []
        exemptions_used = set()
        for source in sorted(_REPO_ROOT.rglob('*.py')):
            relative = source.relative_to(_REPO_ROOT)
            if relative.parts[0] in self.SKIP_TREES:
                continue
            for number, line in enumerate(
                source.read_text(errors='ignore').splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if pattern.search(line):
                    if str(relative) in self.EXEMPT:
                        exemptions_used.add(str(relative))
                    else:
                        offenders.append(f"{relative}:{number}: {stripped[:100]}")

        assert offenders == [], (
            "these lines resolve a root from the environment by hand. "
            "`os.environ.get(name, <default>)` returns '' when the key exists "
            "and is empty (`-e ORCHESTRATOR_ROOT=` on a docker run), so the "
            "default never applies to the case that actually happens, and "
            "Path('') is the CWD (#202). Use config.paths.orchestrator_root(), "
            "orchestrator_state_root(), workspace_root() or root_from_env():\n  "
            + "\n  ".join(offenders)
        )

        assert set(self.EXEMPT) == exemptions_used, (
            "these EXEMPT entries no longer match anything, so they only hide "
            "whatever is added to those files next -- delete them: "
            f"{sorted(set(self.EXEMPT) - exemptions_used)}"
        )


def _import_the_mcp_server_in_a_subprocess(cwd: Path, app_root: str):
    """Import mcp/server.py the way it is actually run and report its paths.

    A SUBPROCESS, and `sys.path.insert(0, <repo>/mcp)` rather than an import of
    `mcp.server`: that directory deliberately has no __init__.py (see the
    module's own docstring) so that it cannot shadow the installed `mcp` SDK,
    which server.py itself imports. `python mcp/server.py` gets the same
    sys.path[0] for free; this reproduces it.

    Measured in switchyard-orchestrator-1: 0.9s per import, ES and FastMCP
    clients included, because every client in that module is lazy.
    """
    environment = dict(os.environ)
    environment['APP_ROOT'] = app_root
    environment['PYTHONPATH'] = str(_REPO_ROOT)
    return subprocess.run(
        [
            sys.executable, '-c',
            'import sys\n'
            f'sys.path.insert(0, {str(_REPO_ROOT / "mcp")!r})\n'
            'import server\n'
            'print("APP_ROOT", server.APP_ROOT)\n'
            'print("STATE_DIR", server.STATE_DIR)\n'
            'print("WORKFLOWS_YAML", server.WORKFLOWS_YAML)\n'
            'print("PROJECTS_CONFIG_DIR", server.PROJECTS_CONFIG_DIR)\n',
        ],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestTheMcpServersDoor:
    """#202's headline shape under a different variable name.

    `APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))`, with WORKFLOWS_YAML,
    PROJECTS_CONFIG_DIR and STATE_DIR all hung off it. `-e APP_ROOT=` on the
    switchyard-mcp container makes that `Path("")`, i.e. the CWD -- verified in
    the container: `Path(os.environ.get("APP_ROOT", "/app")) / "state" /
    "projects"` with APP_ROOT set to the empty string is the relative
    `state/projects`.

    Smaller crater than the orchestrator's seven doors: these three paths are
    only ever read, never mkdir'd, so a bad APP_ROOT degraded to "every MCP tool
    reports no board state" rather than to a write into the wrong tree. It was
    also invisible to the repository-wide tripwire above until APP_ROOT went
    into that alternation, which is why this behavioural pair exists alongside
    it -- a grep that passes while the thing it guards is broken is the failure
    mode this file was written against.
    """

    @staticmethod
    def _paths_from(stdout: str) -> dict:
        return dict(
            line.split(maxsplit=1)
            for line in stdout.splitlines()
            if line.split(maxsplit=1)[:1]
            and line.split(maxsplit=1)[0] in {
                'APP_ROOT', 'STATE_DIR', 'WORKFLOWS_YAML', 'PROJECTS_CONFIG_DIR',
            }
        )

    def test_a_blank_app_root_does_not_become_the_working_directory(self, tmp_path):
        """The accident this issue is named for, in the one file that still had
        it. Run from tmp_path so a regression is visible as a path under it."""
        done = _import_the_mcp_server_in_a_subprocess(tmp_path, app_root='')

        assert done.returncode == 0, done.stderr[-3000:]
        paths = self._paths_from(done.stdout)
        assert len(paths) == 4, f"probe reported {paths}"

        for name, value in paths.items():
            chosen = Path(value)
            assert chosen.is_absolute(), (
                f"mcp/server.py's {name} is {chosen} -- relative, so it "
                f"resolves against the CWD (#202)"
            )
            assert tmp_path not in chosen.parents

        assert paths['APP_ROOT'] == '/app', (
            "a blank APP_ROOT must read as unset and land on the documented "
            f"default, not on {paths['APP_ROOT']}"
        )
        assert paths['STATE_DIR'] == '/app/state/projects'

    def test_a_relative_app_root_is_refused_at_import(self, tmp_path):
        """A dropped leading slash means "under the CWD", and this service's
        CWD is /app. Refusing at import is how the operator finds out."""
        done = _import_the_mcp_server_in_a_subprocess(
            tmp_path, app_root='relative-oops'
        )

        assert done.returncode != 0, (
            f"mcp/server.py imported cleanly with APP_ROOT='relative-oops' and "
            f"bound {done.stdout.strip()!r}"
        )
        assert 'must be an absolute path' in done.stderr, (
            f"it failed for some reason other than the root resolver, so this "
            f"test is no longer measuring what it claims:\n{done.stderr[-2000:]}"
        )
        assert list(tmp_path.iterdir()) == []

    def test_an_absolute_app_root_is_still_honoured(self, tmp_path):
        """The other half: refusing everything would also pass the test above,
        and APP_ROOT is a real override this service documents."""
        scratch = tmp_path / 'scratch'
        scratch.mkdir()

        done = _import_the_mcp_server_in_a_subprocess(tmp_path, app_root=str(scratch))

        assert done.returncode == 0, done.stderr[-3000:]
        paths = self._paths_from(done.stdout)
        assert paths['APP_ROOT'] == str(scratch)
        assert paths['STATE_DIR'] == str(scratch / 'state' / 'projects')
        assert paths['WORKFLOWS_YAML'] == str(
            scratch / 'config' / 'foundations' / 'workflows.yaml'
        )
        assert paths['PROJECTS_CONFIG_DIR'] == str(scratch / 'config' / 'projects')


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
    'validate_artifacts',
    'maintain_agent_team',
    'rebuild_project_images',
]

# Of those, the two that also BIND the resolved root to a module-scope
# constant, so the value itself can be read back. rebuild_project_images is
# absent: it presets ORCHESTRATOR_ROOT into the environment for config_manager
# and resolves inside get_workspace_root(), so it has no such constant. Its
# refusal comes from services.dev_container_state, which it imports -- which is
# why it is in the list above and not this one.
SCRIPTS_THAT_BIND_A_ROOT_AT_IMPORT = [
    'validate_artifacts',
    'maintain_agent_team',
]

# analyze_codebase and generate_artifacts BELONG in both lists above and were in
# them until #199 landed. They are held out here because they cannot currently
# be imported AT ALL, for a reason that has nothing to do with #202:
#
#   claude/claude_integration.py:55  from agents.non_retryable import ...
#     -> agents/__init__.py:9        from .base_maker_agent import MakerAgent
#     -> agents/base_maker_agent.py:17  from claude.claude_integration import
#                                        run_claude_code   # line 55 not reached
#   ImportError: cannot import name 'run_claude_code' from partially
#   initialized module 'claude.claude_integration'
#
# The back-edge at claude_integration.py:55 is new in #199; the six forward
# edges (base_maker_agent and five agent modules) predate it. Any module that
# reaches claude.claude_integration BEFORE it reaches agents/ now dies, which is
# these two scripts and scripts/generate_strategy.py. Reproduced on the
# deployment's own checkout, not just here:
#   docker exec -w /workspace/switchyard switchyard-orchestrator-1 \
#     python -c 'import claude.claude_integration'   -> the ImportError above
# main.py is unaffected because it reaches agents/ first, which is why the
# running orchestrator is healthy and this went unnoticed.
#
# Deliberately NOT fixed here: the fix is either six lazy imports across the
# agent base class and five agents, or relocating NonRetryableAgentError out of
# the agents package, and neither belongs in a state-root PR without its own
# review. Filed rather than folded in.
#
# This exclusion CANNOT quietly outlive its reason. The test below asserts that
# each held-out script still fails for exactly that cycle -- so on the day the
# cycle is fixed, that test goes red and whoever fixed it is told to move these
# two names back into the lists above. The doors in these two files are still
# covered meanwhile by the repository-wide grep tripwire in
# tests/unit/test_state_root_isolation.py, which reads source and needs no
# import; what is suspended is only the dynamic proof.
SCRIPTS_BLOCKED_BY_THE_AGENTS_IMPORT_CYCLE = [
    'analyze_codebase',
    'generate_artifacts',
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

    @pytest.mark.parametrize('module', SCRIPTS_BLOCKED_BY_THE_AGENTS_IMPORT_CYCLE)
    def test_the_held_out_scripts_are_still_held_out_for_the_stated_reason(
        self, module, tmp_path
    ):
        """The expiry date on SCRIPTS_BLOCKED_BY_THE_AGENTS_IMPORT_CYCLE.

        An exclusion list with a prose reason rots the moment the reason stops
        being true, and nothing tells you: the two tests above simply stop
        covering two files and still go green. So assert the reason itself.

        Given a PERFECTLY GOOD absolute root -- the input the test above feeds
        the scripts that work, and the one case where nothing about #202 should
        make a script fail -- these two must still die on the agents/ import
        cycle. When that is fixed they will import cleanly, this test will fail
        on its own first assertion, and the fix is to delete this test and put
        the two names back in the two lists above.

        Asserting the cycle by its exact ImportError, not by returncode: a
        script that started failing for some THIRD reason would otherwise keep
        this test green while its door went unguarded.
        """
        scratch = tmp_path / 'scratch'
        scratch.mkdir()

        done = _import_script_in_a_subprocess(
            module, cwd=tmp_path, root=str(scratch)
        )

        assert done.returncode != 0, (
            f"scripts/{module}.py now imports cleanly, so the agents/ import "
            f"cycle that held it out of SCRIPTS_THAT_RESOLVE_A_ROOT_AT_IMPORT "
            f"and SCRIPTS_THAT_BIND_A_ROOT_AT_IMPORT is fixed. Move "
            f"'{module}' back into both lists and delete this test."
        )
        assert "cannot import name 'run_claude_code'" in done.stderr, (
            f"scripts/{module}.py is failing for something OTHER than the "
            f"documented agents/ import cycle, so the stated reason for "
            f"holding it out is no longer the real one. Re-triage it rather "
            f"than leaving it excluded:\n{done.stderr[-2000:]}"
        )


def _import_the_observability_server_in_a_subprocess(cwd: Path, root: str):
    """Import services/observability_server.py the way the container starts it.

    A SUBPROCESS because the property under test is what happens AT IMPORT, and
    this pytest process has already imported the module (the tests below need
    `_load_github_state`), so an in-process check can only look at names that
    are already bound -- which is exactly the weak guard this replaces.

    `python -c 'import services.observability_server'` rather than `python -m`:
    the module calls `eventlet.monkey_patch()` under `if __name__ ==
    "__main__"`, and monkey-patching is irreversible and process-wide. The
    import chain under test (module scope -> config.state_manager ->
    config.paths) is identical either way, and skipping the patch keeps this
    helper cheap enough to call twice.

    THE PROBE IMPORTS AND PRINTS, AND DOES NOT CALL THE RESOLVER. An earlier
    draft of this helper printed `m.orchestrator_state_root()`, and that call
    raises on a relative root whichever module the name came from -- so the
    bad-root test below passed under the config.paths mutation on the strength
    of the probe's own ValueError, exactly the failure mode it exists to catch
    (measured: mutated, 8 passed / 1 failed; the one that failed was the
    filesystem assertion). What is being measured is whether the IMPORT
    survives, so the probe must do nothing but import.

    Measured in switchyard-orchestrator-1 on this branch: 0.6s per import,
    eventlet/flask/elasticsearch/redis included, because every client in that
    module is constructed lazily.
    """
    environment = dict(os.environ)
    environment['ORCHESTRATOR_ROOT'] = root
    environment['PYTHONPATH'] = str(_REPO_ROOT)
    return subprocess.run(
        [
            sys.executable, '-c',
            'import services.observability_server\n'
            'print("IMPORTED")\n',
        ],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestTheObservabilityServersDoor:
    """It had the same hole in a different shape: a lazy import of
    config.state_manager on a request path, in a container that never built
    that module's singleton, wrapped in `except Exception: return {}`.
    """

    @pytest.fixture(autouse=True)
    def _a_clean_failure_log(self):
        """_state_read_failures_logged is module-scope and survives tests.

        Cleared either side of every test here so that what one asserts about
        the log does not depend on what another already reported.
        """
        from services import observability_server

        observability_server._state_read_failures_logged.clear()
        yield
        observability_server._state_read_failures_logged.clear()

    def test_a_bad_root_kills_the_server_at_boot(self, tmp_path):
        """As it does the orchestrator, which dies at main.py:16.

        main.py imports config.state_manager and therefore dies on a bad
        ORCHESTRATOR_ROOT before anything runs. The observability server had no
        such import -- its only one was inside _load_github_state() -- so it
        started happily and failed per-request, silently, inside `except
        Exception: return {}`.

        ASSERTED AS AN EXIT CODE, not as `hasattr(module,
        'orchestrator_state_root')`, which is what this test used to say. That
        name-presence check passed under a one-line edit a future reader
        deduplicating imports would plausibly make -- swapping the import to
        `from config.paths import orchestrator_state_root`, since config.paths
        is the side-effect-free module and its own docstring invites importing
        it at module scope. Measured with that one-line mutation applied on
        this branch: all eight tests this class then held passed, while
        `ORCHESTRATOR_ROOT=relative-oops python -c 'import
        services.observability_server'` exited 0 instead of raising. The side
        effect IS the property here: it is `import config.state_manager`
        building the GitHubStateManager singleton that calls the resolver, and
        only that import fails at boot. Restoring the original bug shape -- the
        import moved back inside _load_github_state() -- was measured too, and
        fails this test and the next one for the same reason.
        """
        done = _import_the_observability_server_in_a_subprocess(
            tmp_path, root='relative-oops'
        )

        assert done.returncode != 0, (
            "services/observability_server.py imported cleanly with "
            "ORCHESTRATOR_ROOT='relative-oops'. The server will start on a "
            "misconfigured root and fail per-request instead, so the Web UI "
            "shows every project with no board and no repo URL (#202)"
        )
        assert 'must be an absolute path' in done.stderr, (
            "the import failed for some reason other than the root resolver, "
            "so this test is no longer measuring what it claims:\n"
            f"{done.stderr[-2000:]}"
        )
        assert list(tmp_path.iterdir()) == [], (
            "something was created under the CWD on the way to refusing the "
            f"root: {list(tmp_path.iterdir())}"
        )

    def test_an_absolute_root_is_honoured_and_its_state_tree_built(
        self, tmp_path
    ):
        """The other half: refusing every root would also pass the test above.

        The two mkdirs are the assertion, not a side note. `import
        config.state_manager` runs `state_manager = GitHubStateManager()`,
        which mkdirs `<root>/state/projects` and `<root>/state/orchestrator`;
        importing the resolver from config.paths creates nothing. So this
        asserts the module-scope `from config.state_manager import
        orchestrator_state_root` in services/observability_server.py is the
        side-effectful one -- the same mutation the previous test catches by
        exit code, caught here by the filesystem.
        """
        scratch = tmp_path / 'scratch'
        scratch.mkdir()

        done = _import_the_observability_server_in_a_subprocess(
            tmp_path, root=str(scratch)
        )

        assert done.returncode == 0, done.stderr[-2000:]
        assert done.stdout.strip() == 'IMPORTED', done.stdout
        assert (scratch / 'state' / 'projects').is_dir(), (
            "importing services/observability_server.py did not build the "
            "GitHubStateManager singleton, so it is no longer importing "
            "config.state_manager at module scope and a bad root has stopped "
            f"being fatal at boot (#202). Created: {list(scratch.rglob('*'))}"
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

    @staticmethod
    def _corrupt(root: Path, project: str) -> Path:
        """A github_state.yaml whose parse raises. Returns the file."""
        project_dir = root / 'state' / 'projects' / project
        project_dir.mkdir(parents=True, exist_ok=True)
        state_file = project_dir / 'github_state.yaml'
        # Unterminated flow sequence: yaml.safe_load raises ScannerError.
        state_file.write_text('github_state: {boards: [\n')
        return state_file

    @staticmethod
    def _warnings_about(caplog, project: str) -> list:
        return [r for r in caplog.records if project in r.message]

    def test_the_same_failure_on_a_polled_path_is_logged_once_not_every_call(
        self, monkeypatch, tmp_path, caplog
    ):
        """This function is polled, so an unconditional warning is a flood.

        _get_board_url() and _get_repo_url() each call it once per active
        pipeline run, and web_ui/src/routes/dashboard.jsx polls
        /active-pipeline-runs every 10000 ms per open tab -- so one corrupt file
        meant 2N warnings, each with a full traceback, every ten seconds for as
        long as it stayed broken. Measured here: without the dedup these ten
        calls produce ten records; with it, one.
        """
        import logging

        from services import observability_server

        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        self._corrupt(tmp_path, 'a-project')

        with caplog.at_level(logging.WARNING):
            for _ in range(10):
                assert observability_server._load_github_state('a-project') == {}

        assert len(self._warnings_about(caplog, 'a-project')) == 1, (
            f"one corrupt file, ten polls, "
            f"{len(self._warnings_about(caplog, 'a-project'))} warnings: "
            f"{[r.message[:80] for r in caplog.records]}"
        )

    def test_the_one_warning_still_carries_its_traceback(
        self, monkeypatch, tmp_path, caplog
    ):
        """Deduping must not quietly become "log less usefully".

        The point of the exc_info was that a YAML error names a line and column;
        keeping the count at one and dropping the traceback would trade one
        overshoot for the silent failure #202 was about.
        """
        import logging

        from services import observability_server

        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        self._corrupt(tmp_path, 'a-project')

        with caplog.at_level(logging.WARNING):
            observability_server._load_github_state('a-project')

        record, = self._warnings_about(caplog, 'a-project')
        assert record.exc_info is not None, "the first report lost its traceback"

    def test_the_dedup_is_per_project_not_global(
        self, monkeypatch, tmp_path, caplog
    ):
        """Suppressing by exception type alone would hide every project after
        the first, which on a multi-project deployment is the same silence
        #202 removed."""
        import logging

        from services import observability_server

        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        self._corrupt(tmp_path, 'first-project')
        self._corrupt(tmp_path, 'second-project')

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                observability_server._load_github_state('first-project')
                observability_server._load_github_state('second-project')

        assert len(self._warnings_about(caplog, 'first-project')) == 1
        assert len(self._warnings_about(caplog, 'second-project')) == 1

    def test_a_project_that_recovers_and_breaks_again_is_reported_again(
        self, monkeypatch, tmp_path, caplog
    ):
        """Otherwise "log once" means "log once per process lifetime", and the
        second outage of a file that was repaired in between goes unreported
        until someone restarts the server."""
        import logging

        from services import observability_server

        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        state_file = self._corrupt(tmp_path, 'a-project')

        with caplog.at_level(logging.WARNING):
            observability_server._load_github_state('a-project')

            state_file.write_text('github_state:\n  org: an-org\n')
            assert observability_server._load_github_state('a-project') == {
                'org': 'an-org'
            }

            state_file.write_text('github_state: {boards: [\n')
            observability_server._load_github_state('a-project')

        assert len(self._warnings_about(caplog, 'a-project')) == 2, (
            "a repaired-then-rebroken state file was reported "
            f"{len(self._warnings_about(caplog, 'a-project'))} times, not twice"
        )
