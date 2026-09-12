"""The suite has a per-test timeout, and it is live rather than merely written
down (#204).

It was written down and dead for two independent reasons, either sufficient on
its own: `timeout` / `timeout_method` sat under `[pytest:log]`, a section pytest
does not read, and `pytest-timeout` was not installed at all. Fixing one without
the other would have produced a pytest.ini that looks protected and is not --
which is precisely the state #204 was filed about.

So neither test here reads pytest.ini as text. A grep would have passed happily
against the dead config. Instead:

  * TestTheTimeoutIsLiveForThisVeryTest asks the enforcement path itself what it
    would apply to the test that is asking. If the plugin goes away, or the keys
    move back under a section pytest ignores, the resolved value is gone and
    these fail.
  * TestThePluginActuallyKillsAHungTest runs a real pytest on a real sleeping
    test and watches it die. That is the part no amount of config inspection can
    stand in for.
"""

import os
import subprocess
import sys
import time

import pytest

# Imported at module scope on purpose: with pytest-timeout missing this file
# fails at collection with a ModuleNotFoundError naming the package, rather
# than passing vacuously. It is the second line of defence, not the first --
# pytest.ini's `--strict-config` turns the same condition into
# `ERROR: Unknown config option: timeout` and exit 4 with no test run.
# Measured, with the plugin unimportable: that error goes to stderr (so it lands
# on the `collecting ...` line in a terminal and disappears if stdout is piped),
# the suite is still collected in full (`collected 3835 items / 1 error` -- all
# 3848 items at this commit bar this file's own 13, the 1 error being this
# file), and the run phase is what is skipped. So this import raises during that
# same collection pass and
# both defences report together, alongside the third one that survives a pipe:
# tests/conftest.py's report header. This one still matters if someone ever
# drops --strict-config.
import pytest_timeout

# The private resolver pytest-timeout's own `pytest_runtest_protocol` calls to
# decide whether to arm a timer for an item. Asserting on it means asserting on
# the value that is actually enforced, not on a parallel re-derivation of it.
# If a future release renames this, the import fails loudly here rather than
# letting the guard quietly start checking nothing.
from pytest_timeout import _get_item_settings

# The full unit suite measures ~170s (four runs: 167.7/168.2/172.3/170.1s) and
# its slowest single test 45.1s, so
# anything at or below the slowest test would flake and anything above the whole
# suite would be pointless as a bound. The configured value is 180; these are the
# walls it has to stay inside, not a restatement of it.
SLOWEST_MEASURED_TEST_SECONDS = 45.2
FULL_UNIT_SUITE_SECONDS = 170.0

# The headroom multiple pytest.ini argues for and sizes `timeout` by: 4x the
# slowest measured test, which is real headroom rather than a guess because
# that test's 45s is a fixed wall-clock budget (four runs: 45.07/45.14/45.15/
# 45.14s) rather than CPU work that stretches on slower hardware.
#
# The upper guard below asserts exactly this multiple rather than something
# looser, because a looser bound does not defend the decision it documents.
# The dead value #204 removed was 300s, and an earlier version of that guard
# read `timeout <= 2 * FULL_UNIT_SUITE_SECONDS` (324s) -- mutation-tested, it
# passed at `-o timeout=300` and only started failing at 325, i.e. someone
# could restore the exact value the issue calls useless and this file would
# still go green. 300s is 6.6x the slowest test; 180s is 3.98x.
MAX_HEADROOM_OVER_SLOWEST_TEST = 4


def _resolved_timeout(node):
    """The timeout pytest-timeout would arm for `node`, or a failure saying so.

    Going through this rather than comparing `settings.timeout` directly:
    when the key is dead the resolver returns None, and a bare `None > 45.2`
    fails with `TypeError: '>' not supported between instances of 'NoneType'
    and 'float'` -- a guard that fires for the right cause while reporting the
    wrong one.
    """
    timeout = _get_item_settings(node).timeout
    if timeout is None:
        pytest.fail(
            'pytest-timeout resolved no timeout for this test, so there is '
            'nothing to range-check. The `timeout` key is missing, empty, or '
            'in a section pytest does not read.'
        )
    return timeout


class TestTheTimeoutIsLiveForThisVeryTest:

    def test_the_plugin_is_installed_and_registered(self, pytestconfig):
        """Reason two of the two. `pytest-timeout` absent means pytest does not
        even recognise the ini key."""
        assert pytestconfig.pluginmanager.hasplugin('timeout'), (
            'pytest-timeout is not registered with this pytest run, so the '
            '`timeout` key in pytest.ini is enforcing nothing.'
        )

    def test_a_timeout_is_resolved_for_the_running_item(self, request):
        """Reason one of the two. Keys under `[pytest:log]` resolve to None here
        even with the plugin installed and happy."""
        settings = _get_item_settings(request.node)

        assert settings.timeout is not None, (
            'pytest-timeout resolved no timeout for this test. The `timeout` '
            'key is either missing or in a section pytest does not read.'
        )
        assert settings.timeout > 0, (
            f'timeout resolved to {settings.timeout!r}; 0 or negative disables '
            'the timeout entirely.'
        )

    def test_the_value_is_above_the_slowest_real_test(self, request):
        """Below the slowest legitimate test, the timeout is a flake generator
        rather than a backstop."""
        timeout = _resolved_timeout(request.node)

        assert timeout > SLOWEST_MEASURED_TEST_SECONDS, (
            f'timeout={timeout}s is not above the slowest measured test '
            f'({SLOWEST_MEASURED_TEST_SECONDS}s), so that test would be killed '
            'while behaving correctly.'
        )

    def test_the_value_is_not_so_large_it_stops_bounding_anything(self, request):
        """Past a few multiples of the slowest real test the number stops being
        a bound on anything a human would wait for. The previous dead value was
        300s: 6.6x the slowest test and 1.85x the entire suite, for one wedged
        test. This asserts the 4x headroom pytest.ini actually argues for, so
        restoring 300 fails here -- mutation-tested both ways."""
        timeout = _resolved_timeout(request.node)
        ceiling = MAX_HEADROOM_OVER_SLOWEST_TEST * SLOWEST_MEASURED_TEST_SECONDS

        assert timeout <= ceiling, (
            f'timeout={timeout}s is more than {MAX_HEADROOM_OVER_SLOWEST_TEST}x '
            f'the slowest measured test ({SLOWEST_MEASURED_TEST_SECONDS}s), i.e. '
            f'above the {ceiling}s ceiling pytest.ini sizes the value by, and '
            f'{timeout / FULL_UNIT_SUITE_SECONDS:.2f}x a clean run of the whole '
            f'{FULL_UNIT_SUITE_SECONDS}s suite. If a slower test is now '
            'legitimate, re-measure it and move SLOWEST_MEASURED_TEST_SECONDS; '
            'if the headroom multiple itself should change, change it here and '
            'in pytest.ini together.'
        )

    # There is deliberately no "the method is one pytest-timeout implements"
    # test. An earlier draft had one on the assumption that a typo would be
    # another silently-dead setting; measured, it is not. `-o
    # timeout_method=threed` never reaches collection -- pytest-timeout's
    # pytest_configure raises `ValueError: Invalid method threed from config
    # file` and the run ends in INTERNALERROR. A guard that cannot run when the
    # thing it guards is broken guards nothing.

    def test_the_method_is_thread(self, request):
        """`thread` is not the plugin default -- pytest_timeout.py picks
        `signal` wherever SIGALRM exists -- so this pins a deliberate override,
        and pytest.ini carries the measurements behind it.

        The short version, because the long one used to be wrong here: on the
        RecordedThread/ThreadPoolExecutor wedge #204 is about, the two methods
        give the *same* diagnosis (`Failed: Timeout (>3.0s)` at
        threading.py:327 `waiter.acquire()`), both dump every thread's stack,
        and neither bounds a C-level loop holding the GIL. The one measured
        difference that favours `thread` is that `signal` raises the timeout
        *into the test*, so a retry loop swallowing BaseException absorbs it
        and hangs on regardless; `thread`'s timer needs no cooperation and
        killed that case at the limit.

        Change this if the tradeoff is reconsidered -- `thread` calls
        os._exit(1), so the rest of the run is abandoned, the report-only CI job
        loses its results, and every fixture teardown is skipped, which costs
        tests/conftest.py's session-autouse cleanup_test_data its exit-side
        purge of the global rate-limit keys (measured: a marker fixture wrote
        `pre` and `post` under signal, only `pre` under thread) -- but change it
        knowing those are the trades, and re-measure rather than reasoning about
        it. pytest.ini carries the numbers and
        test_service_client_fail_fast.py guards the entry-side purge that is the
        recovery.
        """
        assert _get_item_settings(request.node).method == 'thread'


class TestThePluginActuallyKillsAHungTest:
    """An out-of-process proof. Everything above is still, ultimately, reading
    configuration; this watches a test that will never finish get killed."""

    SLEEP_SECONDS = 60
    CHILD_TIMEOUT = 2
    # Generous: the child should die at ~2s plus interpreter start. Anything
    # near this number means it was not killed by the plugin.
    WALL_CLOCK_BUDGET = 30

    def _run_child(self, tmp_path, method):
        (tmp_path / 'test_wedged.py').write_text(
            'import time\n'
            '\n'
            '\n'
            'def test_never_finishes():\n'
            f'    time.sleep({self.SLEEP_SECONDS})\n'
        )
        # A standalone ini, not this repo's: the point is to prove the installed
        # plugin enforces using the method this repo configures, without waiting
        # out the repo's own (correctly large) 180s.
        (tmp_path / 'child.ini').write_text(
            '[pytest]\n'
            f'timeout = {self.CHILD_TIMEOUT}\n'
            f'timeout_method = {method}\n'
        )

        env = dict(os.environ)
        # Would silently alter the child's config and, for PYTEST_TIMEOUT,
        # override the very thing under test.
        env.pop('PYTEST_ADDOPTS', None)
        env.pop('PYTEST_TIMEOUT', None)

        started = time.monotonic()
        completed = subprocess.run(
            [
                sys.executable, '-m', 'pytest',
                '-c', 'child.ini',
                '--rootdir', str(tmp_path),
                '-p', 'no:randomly',
                '-p', 'no:cacheprovider',
                '-q',
                'test_wedged.py',
            ],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            # Backstop on the guard itself. If the plugin does not work, this
            # raises TimeoutExpired and the test fails loudly instead of
            # inheriting the very hang it exists to rule out.
            timeout=self.WALL_CLOCK_BUDGET,
        )
        return completed, time.monotonic() - started

    def test_a_sleeping_test_is_killed_well_before_it_finishes(
        self, tmp_path, request
    ):
        method = _get_item_settings(request.node).method
        completed, elapsed = self._run_child(tmp_path, method)
        output = completed.stdout + completed.stderr

        assert completed.returncode != 0, (
            f'a test sleeping {self.SLEEP_SECONDS}s under a '
            f'{self.CHILD_TIMEOUT}s timeout exited 0:\n{output}'
        )
        assert elapsed < self.SLEEP_SECONDS, (
            f'the child ran {elapsed:.1f}s, i.e. it was allowed to finish its '
            f'{self.SLEEP_SECONDS}s sleep rather than being killed.'
        )
        assert 'Timeout' in output, (
            'the child died without pytest-timeout saying so, so something '
            f'other than the timeout killed it:\n{output}'
        )

    def test_the_kill_names_the_line_that_hung(self, tmp_path, request):
        """The whole value of a timeout over a wedged CI job is the diagnosis it
        leaves behind. A kill with no pointer to the hung line is barely better
        than the hang."""
        method = _get_item_settings(request.node).method
        completed, _ = self._run_child(tmp_path, method)
        output = completed.stdout + completed.stderr

        assert 'test_wedged.py' in output, (
            f'the timeout report does not name the offending file:\n{output}'
        )
        assert 'test_never_finishes' in output or 'time.sleep' in output, (
            f'the timeout report does not name the offending test or the call '
            f'it hung in:\n{output}'
        )


class TestAMissingPluginSaysHowToFixIt:
    """`pytest-timeout` is new in #204 and `--strict-config` makes its absence
    fatal, so the first run of this suite inside any image built before #204
    merged collects the whole suite and then exits 4 with nothing run.

    That is the right shape -- a timeout that quietly evaporates is the bug #204
    was filed about -- but on its own it says nothing about the cause. Measured
    in the orchestrator container with the plugin unimportable, the operator
    gets `ERROR: Unknown config option: timeout` on **stderr** (gone the moment
    stdout is piped) and a `ModuleNotFoundError` from this file's import during
    collection. Neither mentions rebuilding an image, which is the only fix.

    tests/conftest.py's report header closes that gap. These three tests guard
    it: that it stays quiet while the plugin is registered, that it produces the
    remedy when it is not, and that pytest still reaches the hook on the exit-4
    path -- a hook pytest never calls would be a guard that reads correct and
    shows nobody anything. Mutation-tested: dropping the append in
    pytest_report_header leaves the middle one passing and fails only the third,
    which is why the third is not redundant.
    """

    # Same lever `--strict-config` reacts to and the same lever the header
    # keys off: measured, `-p no:timeout` against this repo's pytest.ini gives
    # `ERROR: Unknown config option: timeout`, exit 4, `no tests ran`. It is a
    # faithful stand-in for "not installed" that works without uninstalling
    # anything, so it runs in CI as well as here.
    DISABLE_PLUGIN = ('-p', 'no:timeout')

    # Words the remedy has to actually contain. A header that fires on the
    # right condition and then fails to name the fix is the failure mode this
    # class exists to prevent, so assert the substance, not just non-emptiness.
    REQUIRED_PHRASES = ('requirements.txt', 'docker compose build orchestrator')

    def test_the_header_is_silent_while_the_plugin_is_present(self, pytestconfig):
        """The live config of the run executing this line. Ties the shim below
        to reality: if `hasplugin('timeout')` ever stops being the question that
        distinguishes present from absent, this starts failing on every green
        run rather than letting the shim drift into testing nothing."""
        from tests.conftest import timeout_plugin_missing_header

        assert pytestconfig.pluginmanager.hasplugin('timeout')
        assert timeout_plugin_missing_header(pytestconfig) is None, (
            'the header is warning about a missing pytest-timeout during a run '
            'in which pytest-timeout is demonstrably registered.'
        )

    def test_the_header_names_the_remedy_when_the_plugin_is_absent(self):
        """The condition itself cannot be created in-process -- unregistering a
        plugin mid-run does not un-parse the ini keys -- so this drives the hook
        with a plugin manager reporting what the real one reports under
        `-p no:timeout`. The out-of-process test below is what proves that
        report and the real failure are the same event."""
        from tests.conftest import timeout_plugin_missing_header

        class _NoTimeoutPlugin:
            def hasplugin(self, name):
                return name != 'timeout'

        class _Config:
            pluginmanager = _NoTimeoutPlugin()

        line = timeout_plugin_missing_header(_Config())

        assert line is not None, (
            'pytest-timeout is unregistered and the header said nothing.'
        )
        for phrase in self.REQUIRED_PHRASES:
            assert phrase in line, (
                f'the header fires but does not mention {phrase!r}, so it '
                f'reports the symptom without the fix:\n{line}'
            )

    def test_the_header_reaches_the_screen_on_the_strict_config_exit(self):
        """The part no in-process call can stand in for. `--strict-config` ends
        the run at the config layer, and a header hook that pytest skips on that
        path would leave the guard above passing while the operator sees only
        the stderr line.

        Measured: pytest prints the whole header block, reports `collected 0
        items`, and exits 4. `tests/conftest.py` is the collection target on
        purpose -- it loads this repo's conftest (which is what is under test)
        without collecting any test file, so nothing here couples to another
        file's contents.
        """
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        )))

        env = dict(os.environ)
        env.pop('PYTEST_ADDOPTS', None)
        env.pop('PYTEST_TIMEOUT', None)

        completed = subprocess.run(
            [
                sys.executable, '-m', 'pytest',
                '-p', 'no:randomly',
                '-p', 'no:cacheprovider',
                *self.DISABLE_PLUGIN,
                '--collect-only', '-q',
                os.path.join('tests', 'conftest.py'),
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert completed.returncode == 4, (
            f'expected --strict-config to exit 4 with pytest-timeout '
            f'unregistered, got {completed.returncode}:\n'
            f'{completed.stdout}\n{completed.stderr}'
        )
        # Deliberately stdout only. The point of the header is that it survives
        # `... | tee`, which the stderr line does not.
        assert 'pytest-timeout: NOT REGISTERED' in completed.stdout, (
            'the child exited 4 for exactly this reason and printed no header '
            f'saying so on stdout:\n{completed.stdout}'
        )
        for phrase in self.REQUIRED_PHRASES:
            assert phrase in completed.stdout, (
                f'the header reached stdout without mentioning {phrase!r}:\n'
                f'{completed.stdout}'
            )


class TestTheSuiteStillWritesNoLogFileIntoTheCheckout:
    """`log_file = tests/test_run.log` lived in the same dead `[pytest:log]`
    section and was dropped rather than moved (#204, and #181 before it: the
    suite must not write into the tree it is running from). Measured by running
    tests/unit once with it restored: 3.8MB over 33,440 lines, per run, inside
    the checkout, and invisible to `git status` because `.gitignore` covers
    `*.log`.

    This reads the live config, not the text of pytest.ini -- restoring the key
    under `[pytest]` flips it, which is how it was checked."""

    def test_no_log_file_sink_is_configured(self, pytestconfig):
        assert not pytestconfig.getini('log_file'), (
            f'log_file is set to {pytestconfig.getini("log_file")!r}; the suite '
            'would write a DEBUG log into the checkout on every run.'
        )


def test_the_timeout_marker_survives_strict_markers():
    """`addopts` carries `--strict-markers`, so an unregistered marker is a
    collection error. pytest-timeout registers `timeout` in its own
    `pytest_configure`; if that ever stopped, every documented per-test override
    (`@pytest.mark.timeout(600)`) would become an error instead of an escape
    hatch."""
    assert hasattr(pytest.mark, 'timeout')
    assert pytest_timeout.__name__ == 'pytest_timeout'


@pytest.mark.timeout(SLOWEST_MEASURED_TEST_SECONDS + 5)
def test_a_per_test_override_is_honoured(request):
    """The escape hatch pytest.ini points tests/integration and tests/e2e at.
    Exercising it here also proves `--strict-markers` accepts the marker, which
    is the part that would break first."""
    assert _get_item_settings(request.node).timeout == pytest.approx(
        SLOWEST_MEASURED_TEST_SECONDS + 5
    )
