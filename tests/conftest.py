"""
Pytest configuration and shared fixtures

This file provides common fixtures and configuration for all tests.
"""

import pytest
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)


# ============================================================================
# Orchestrator container detection
# ============================================================================

# What the ~60 container-gated test files test for. Defined up here rather
# than beside CONTAINER_ONLY_SKIP_REASON below because the #174 guards that
# follow are the first thing this module does and every one of them asks it.
ORCHESTRATOR_CONTAINER_MARKER = '/app'


def running_in_orchestrator_container():
    return os.path.isdir(ORCHESTRATOR_CONTAINER_MARKER)


# ============================================================================
# Service-client fail-fast (#174)
# ============================================================================

# The docker-compose service names this codebase connects to by bare hostname.
# Off-container none of them is a real host, and what that costs is measured in
# _refuse_to_resolve_compose_service_hostnames() below.
COMPOSE_SERVICE_HOSTS = ('redis', 'elasticsearch', 'otel-collector')

# Escape hatch for a runner that DOES provide these as real service containers
# (GitHub Actions `services:` maps a service named `redis` to that hostname).
# Nothing in this repo's workflow does today -- see .github/workflows/unit-tests.yml.
ALLOW_REAL_SERVICE_HOSTS = os.environ.get('SWITCHYARD_TEST_ALLOW_SERVICE_HOSTS') == '1'

# What a Redis/Elasticsearch connect is allowed to cost during a test run, in
# seconds. Overridable per-run for the rare test that genuinely wants to sit
# through a slow service.
TEST_SERVICE_CONNECT_TIMEOUT = float(os.environ.get('SWITCHYARD_TEST_CONNECT_TIMEOUT', '2'))

# ...and what a single command against a connected service may cost. Far above
# anything the suite's own Redis/ES work takes in the orchestrator container
# (single small hashes, one delete_by_query), so this bounds a wedged service
# rather than a slow one.
TEST_SERVICE_OP_TIMEOUT = float(os.environ.get('SWITCHYARD_TEST_OP_TIMEOUT', '10'))


def _refuse_to_resolve_compose_service_hostnames():
    """
    Off-container, make `redis`/`elasticsearch`/`otel-collector` fail to
    resolve at once instead of asking the resolver, so a connect to a service
    that is not running fails immediately (#174).

    Half of the fix; _bound_service_client_timeouts() below is the other half,
    and neither is what the symptom suggests. On a bare python:3.11-slim runner
    with only `pip install -r requirements.txt`, `pytest tests/unit` did not
    finish -- it reached services/cancellation.py's `redis.Redis(host='redis',
    port=6379, decode_responses=True).ping()` and sat there, ~6s of CPU over 3+
    minutes at ~0.2%. pytest.ini's `timeout = 300` does not bound it, because
    pytest-timeout cannot interrupt a blocking socket call in the main thread.

    The obvious reading is a missing socket_connect_timeout, and ~20 call sites
    across services/, monitoring/, claude/ and task_queue/ do omit it. Timed in
    that container, it is not that at all:

        redis.Redis(host='redis', ...).ping()                    -> 5.04s
        redis.Redis(host='redis', ..., socket_connect_timeout=2) -> 4.16s

    Two independent costs, and redis-py 8 already defaults
    socket_connect_timeout to 5 regardless. This function addresses the first:
    `socket.getaddrinfo`, which no client-level timeout bounds. A bare name plus
    the runner's `search` suffixes is several queries against whatever
    nameservers it was handed, and how long they take to give up is a property
    of that machine -- which is why the same suite took 5 seconds one hour and
    had not finished in 150 the next, and why this was never reproducible
    enough to pin down from the symptom. (The second cost is redis-py's default
    retry policy; see the other function.)

    Refusing the name rather than re-pointing it (found in review). The first
    version of this mapped all three to 127.0.0.1, reasoning that loopback with
    nothing listening is ECONNREFUSED in microseconds. Nothing is listening
    only if the developer is NOT running the stack: docker-compose.yml
    publishes redis as "6379:6379" and elasticsearch as "9200:9200", so on the
    very machine a host run is meant to help, 127.0.0.1:6379 IS the live
    deployment's Redis. That turned a guaranteed-safe failure into a
    guaranteed connection to production -- for cleanup_test_data()'s purge
    below, and for every client in the suite that is constructed rather than
    injected (services/cancellation.py's _get_redis(), the lock and semaphore
    managers, the circuit breakers, task_queue).

    socket.gaierror costs the same (no resolver, no connect), cannot collide
    with whatever the host happens to be running, and is exactly what these
    names did off-container before any of this existed. It is an OSError, so
    redis-py surfaces it as redis.ConnectionError and elastic_transport as its
    own ConnectionError -- an ordinary "service is down" to the code under
    test, which is what it should see.

    Only these three names are affected, and only when /app is absent --
    inside the orchestrator container they are real hosts that the suite
    genuinely uses.
    """
    if running_in_orchestrator_container() or ALLOW_REAL_SERVICE_HOSTS:
        return

    import socket

    original = socket.getaddrinfo
    if getattr(original, '_switchyard_refuses_service_hosts', False):
        return

    def refusing_getaddrinfo(host, *args, **kwargs):
        if host in COMPOSE_SERVICE_HOSTS:
            raise socket.gaierror(
                socket.EAI_NONAME,
                f"switchyard test guard: '{host}' is a docker-compose service name "
                f"and does not resolve outside the orchestrator container. Set "
                f"SWITCHYARD_TEST_ALLOW_SERVICE_HOSTS=1 if this runner really does "
                f"provide it."
            )
        return original(host, *args, **kwargs)

    refusing_getaddrinfo._switchyard_refuses_service_hosts = True
    socket.getaddrinfo = refusing_getaddrinfo


def _bound_service_client_timeouts():
    """
    Give every Redis and Elasticsearch client built during a test run a bounded
    connect and a no-retry policy (#174).

    The guard above removes the resolver from the path; this removes the retry
    loop that sits on top of it. Both are needed and neither is sufficient:
    with the name resolved instantly and the default retry policy in place, a
    ping to an absent Redis still cost 4.5 seconds, times however many
    constructions a run makes (services/cancellation.py's _get_redis()
    reconnects on every call). This also covers what the name guard does not --
    a runner that DOES provide these as real service containers, i.e. the
    SWITCHYARD_TEST_ALLOW_SERVICE_HOSTS=1 case -- and Elasticsearch's own
    default of retrying a dead node.

    Done here rather than at the ~20 call sites that omit these deliberately.
    Their defaults are right for production, where the service genuinely is
    there and a fast give-up would turn a slow boot into a degraded
    orchestrator; the hang is a property of the TEST environment, so the fix
    belongs in the test harness. One hook also covers constructions added later
    and ones made inside libraries, which a call-site sweep cannot.

    Only fills in values the caller did not set, so a test that sets its own
    timeouts keeps them. Applies in the orchestrator container too, where Redis
    and ES are reachable and a connect costs single-digit milliseconds --
    container and host runs stay comparable, which is the point of the exercise.
    """
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.connection import AbstractConnection
        from redis.retry import Retry
    except Exception:
        pass
    else:
        # `retry` is the one that matters, and it is not the one anybody would
        # guess. redis-py 8 defaults a client to Retry(ExponentialWithJitter
        # Backoff(base=1, cap=10), retries=10), so a connect to a host that
        # fails instantly is still attempted eleven times with a growing
        # sleep between them. Measured on the bare runner: ping() against an
        # absent Redis took 4.5s with the resolution itself costing 0.0s, and
        # 0.02s with Retry(NoBackoff(), 0).
        redis_defaults = dict(
            socket_connect_timeout=TEST_SERVICE_CONNECT_TIMEOUT,
            socket_timeout=TEST_SERVICE_OP_TIMEOUT,
            retry=Retry(NoBackoff(), 0),
        )
        # Both layers: Redis.__init__ has its own non-None defaults (5s connect
        # in redis-py 8) which it passes down explicitly, so a default filled in
        # only at the connection layer would never be reached from the ordinary
        # `redis.Redis(host=...)` path -- while Redis.from_url() and an
        # explicitly built ConnectionPool skip Redis.__init__'s kwargs and only
        # the connection layer sees them.
        _patch_init_defaults(redis.Redis, **redis_defaults)
        _patch_init_defaults(AbstractConnection, **redis_defaults)

    try:
        from elasticsearch import Elasticsearch
    except Exception:
        pass
    else:
        # max_retries/retry_on_timeout do the work here: the default is to
        # retry a failed node, which multiplies the wait by the number of
        # attempts against a host that will never answer, while a name the
        # guard above refuses fails immediately either way.
        # request_timeout therefore gets the OPERATION budget, not the connect
        # one -- it bounds the whole request, and a real ES query inside the
        # orchestrator container (conftest's own delete_by_query sweep, for
        # one) can legitimately take longer than a connect may.
        _patch_init_defaults(
            Elasticsearch,
            request_timeout=TEST_SERVICE_OP_TIMEOUT,
            max_retries=0,
            retry_on_timeout=False,
        )


def _patch_init_defaults(cls, **defaults):
    """Wrap cls.__init__ so `defaults` fill in for keywords the caller omitted.

    Idempotent: pytest_configure runs once per process, but a nested pytest
    invocation (tests/unit/test_container_gated_reporting.py runs one) would
    otherwise stack wrappers.
    """
    original = cls.__init__
    if getattr(original, '_switchyard_bounded', False):
        return

    def bounded_init(self, *args, **kwargs):
        for key, value in defaults.items():
            kwargs.setdefault(key, value)
        return original(self, *args, **kwargs)

    bounded_init._switchyard_bounded = True
    cls.__init__ = bounded_init


# The deployment's own directory, reused from the container marker above rather
# than re-spelled: if /app ever stops being what identifies this container, the
# guard below must move with it.
DEPLOYMENT_DIR = Path(ORCHESTRATOR_CONTAINER_MARKER)


def _refuse_a_root_that_can_reach_the_deployment(value: str, source: str) -> str:
    """Reject a caller-supplied state root that would write into production.

    This check used to live in
    tests/unit/test_state_root_isolation.py::test_the_active_root_is_not_the_
    deployments_directory_under_any_alias, where it DETECTED rather than
    prevented: a test runs partway through the session, so everything
    alphabetically before it has already written wherever the bad root pointed
    (#202). Here it runs at conftest import, before a single module is
    imported, and raises.

    Three refusals, in the order they can bite:

    1. Not absolute. `orchestrator_state_root()` refuses these too, but only
       when something calls it -- a relative root would otherwise be accepted
       here and then blow up module by module. `-e ORCHESTRATOR_ROOT=tmp/x`, a
       dropped leading slash, is the documented command's likeliest typo, and
       the CWD it would resolve against is the checkout.

    2. The deployment directory under any alias. Compare identity, not
       spelling: on the deployment `/app` and `/workspace/switchyard` are the
       SAME INODE (docker-compose mounts the checkout twice), so `!= '/app'`
       passes for `/workspace/switchyard`, for `/app/../app`, and for a
       bind-mounted worktree path that lands on the same inode.

    3. ANYTHING UNDER the deployment directory. Stricter than the test this
       replaces, deliberately: the next thing the caller does with an accepted
       root is `mkdir(parents=True)`, so `/app/scratch` does not merely read
       production, it CREATES a directory in it -- and `/app/state` would hand
       the suite the live tree outright. Walking the resolved path's ancestors
       catches those through every alias too, because the ancestors are
       compared by identity as well.

    Skips 2 and 3 where /app is not a directory: off-container there is no
    deployment to collide with, and every path would otherwise be compared
    against a stat() that fails.
    """
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise RuntimeError(
            f"{source}={value!r} is relative. A relative state root resolves "
            f"against the current working directory, which for the documented "
            f"invocation is the checkout itself -- so the suite would write "
            f"into the deployment (#181). Use an absolute path."
        )

    if not DEPLOYMENT_DIR.is_dir():
        return str(candidate)

    deployment = DEPLOYMENT_DIR.stat()
    deployment_identity = (deployment.st_dev, deployment.st_ino)

    resolved = candidate.resolve()
    for ancestor in (resolved, *resolved.parents):
        try:
            info = ancestor.stat()
        except OSError:
            # Does not exist yet (the caller may be about to mkdir it) or is
            # unreadable. Either way it is not the deployment directory.
            continue
        if (info.st_dev, info.st_ino) == deployment_identity:
            where = "is" if ancestor == resolved else f"is under {ancestor}, which is"
            raise RuntimeError(
                f"{source}={value!r} {where} the deployment directory "
                f"({DEPLOYMENT_DIR}) under another name. The suite would write "
                f"into live orchestrator state -- that is exactly #181. Point "
                f"it somewhere outside the checkout, e.g. a directory under "
                f"/tmp."
            )

    return str(candidate)


def _redirect_orchestrator_root_to_scratch():
    """
    Point ORCHESTRATOR_ROOT at a scratch directory for EVERY test run, so the
    suite cannot write into a real `state/` tree.

    Off-container: services/dev_container_state.py and
    services/work_execution_state.py construct their singleton at import time,
    and that constructor does `Path(os.environ.get('ORCHESTRATOR_ROOT', '/app'))
    / "state" / ...` followed by mkdir(parents=True) -- which fails on any
    machine without a writable /app. Three test files worked around that by
    assigning a MagicMock into sys.modules at module scope and never removing
    it; see tests/unit/test_pr_review_phase_recovery.py (#133) for what that
    cost.

    ON-container (#181): this used to return early, reasoning that "/app/state
    IS the state directory those modules are supposed to read". That was
    backwards. The documented way to run this suite is `pytest tests/unit` from
    the repository root, and on the deployment the repository root IS the
    directory bind-mounted at /app -- so the suite wrote its fixtures into the
    LIVE state tree and the production watchdog then did real work on them.
    Seventeen files were observed reappearing after a verified-clean deletion,
    every one timestamped to a test run rather than to the orchestrator.

    No test needs the live tree: each either builds its own manager against
    tmp_path or exercises a singleton whose content it also wrote, and a test
    asserting on whatever the deployment happens to hold right now would be
    untrustworthy anyway. SWITCHYARD_TEST_STATE_ROOT is the escape hatch if one
    ever genuinely does.

    An explicitly-set ORCHESTRATOR_ROOT still wins, so the invocation in
    CLAUDE.md (`docker exec -e ORCHESTRATOR_ROOT=/tmp/... ... pytest`) is
    unchanged -- it is simply no longer the only thing standing between the
    suite and production. It no longer wins UNVALIDATED, though: it used to be
    accepted exactly as given, so `ORCHESTRATOR_ROOT=<the checkout>` ran the
    whole suite against live state with all of it green (#202). Both preset
    paths now go through _refuse_a_root_that_can_reach_the_deployment().

    A preset that is empty or whitespace-only is NOT honoured as "unset" here
    and falls through to the scratch branch. orchestrator_state_root() reads
    those as unset and falls back to the checkout -- which is the deployment --
    so honouring them the same way would point the suite straight at
    production.
    """
    preset = os.environ.get('ORCHESTRATOR_ROOT')
    if preset and preset.strip():
        os.environ['ORCHESTRATOR_ROOT'] = _refuse_a_root_that_can_reach_the_deployment(
            preset.strip(), 'ORCHESTRATOR_ROOT'
        )
        return

    override = os.environ.get('SWITCHYARD_TEST_STATE_ROOT')
    if override and override.strip():
        override = _refuse_a_root_that_can_reach_the_deployment(
            override.strip(), 'SWITCHYARD_TEST_STATE_ROOT'
        )
        # Validated here, because the mkdtemp branch below guarantees an
        # existing writable directory and this one guaranteed nothing. An
        # unusable value surfaced as a FileNotFoundError from some unrelated
        # singleton's import-time mkdir, naming neither environment variable.
        try:
            Path(override).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(
                f"SWITCHYARD_TEST_STATE_ROOT={override!r} is not usable as a "
                f"state root: {e}"
            ) from e
        os.environ['ORCHESTRATOR_ROOT'] = override
        return

    import tempfile
    os.environ['ORCHESTRATOR_ROOT'] = tempfile.mkdtemp(prefix='switchyard-test-root-')


DEPLOYMENT_TUNING_ENV_VARS = (
    'USE_BATCHED_BOARD_QUERIES',
    'WATCHDOG_MAX_RETRIES',
    'WATCHDOG_MAX_RECORD_AGE_HOURS',
    'RECONCILIATION_FRESHNESS_HOURS',
    'TOKEN_METRICS_INTERVAL_HOURS',
    'PROGRAMMATIC_CHANGE_WINDOW_SECONDS',
    'DOCKER_SOCKET_ACCESS_MAX_CONCURRENT',
    'DOCKER_SOCKET_ACCESS_MAX_WAIT_SECONDS',
)


def _clear_deployment_tuning_env_vars():
    """
    Remove the deployment's rollout/tuning knobs so unit tests see the code's
    own defaults.

    These are set on the orchestrator container by docker-compose, and the
    documented way to run this suite is inside that container -- so a test
    asserting "this feature is off unless someone turns it on" was really
    asserting "this feature is off on whatever machine happens to run me".
    USE_BATCHED_BOARD_QUERIES=true is set there today, and it failed
    test_project_monitor_batched_polling.py::test_defaults_off and
    test_project_monitor_failsafe_batching.py::TestFailsafeBatchedGathering
    Unflagged in the container while both passed on a host.

    An explicit list, not a pattern or a blanket scrub. Credentials, service
    hostnames and ORCHESTRATOR_ROOT are deliberately NOT in it: tests that
    need them need the real ones, and the guards above already handle the
    hostnames. Only knobs whose *default value* is itself under test belong
    here -- a test that wants one set uses patch.dict, as
    test_enabled_via_env_var already does.
    """
    for name in DEPLOYMENT_TUNING_ENV_VARS:
        os.environ.pop(name, None)


def _install_a_disabled_observability_singleton():
    """
    Stop the suite writing telemetry into the deployment's Elasticsearch.

    get_observability_manager() lazily builds ObservabilityManager(enabled=True),
    which connects to REDIS_HOST/ELASTICSEARCH_HOST -- inside the orchestrator
    container those are the LIVE services. Any test that reaches an emit() then
    publishes to the real event stream and indexes a document into the real
    agent-events-*/decision-events-* indices. Those documents are built from
    whatever the test passed in, so they are not merely extra: they are wrong.

    It is also slow in a way that reads as a hang. es_index_with_retry() retries
    5 times with 2/4/8/16s backoff, so one rejected document costs 30 seconds of
    time.sleep() inside a test. Four tests in test_workspace_contexts.py were
    paying exactly that -- 150s for a 10-test file.

    enabled=False short-circuits emit() on its first line and skips client
    construction entirely, so this is also strictly faster than connecting.

    Tests that actually exercise indexing are unaffected: they construct their
    own ObservabilityManager with mock redis/es clients (see
    tests/unit/test_observability_elasticsearch.py) rather than going through
    this singleton. Anything that wants to assert on emits through the
    singleton should patch it, which the fixtures that care already do.
    """
    import monitoring.observability as observability
    observability._observability_manager = observability.ObservabilityManager(
        enabled=False
    )


def _install_a_mock_backed_pipeline_run_manager_singleton():
    """
    Stop the suite mutating the deployment's Elasticsearch *schema*, which the
    observability guard above does not cover.

    services.pipeline_run._pipeline_run_manager is a SECOND, independent
    get-or-create module global, so disabling the observability singleton does
    nothing for it. PipelineRunManager.__init__ builds its own
    redis.Redis(host='redis') and Elasticsearch("http://elasticsearch:9200")
    and then calls _setup_elasticsearch() EAGERLY -- two unconditional writes
    to the live cluster before the object is even returned:

        es.ilm.put_lifecycle(name="pipeline-runs-ilm-policy", ...)
        es.indices.put_index_template(name="pipeline-runs-template", ...)

    and ProjectMonitor.__init__ calls get_pipeline_run_manager(), so merely
    CONSTRUCTING a ProjectMonitor in a unit test performed them. Verified
    against the live cluster before this guard existed: importing
    tests.conftest and constructing one ProjectMonitor put
    pipeline-runs-ilm-policy.

    This is worse than the junk documents the observability guard stops.
    Those are rows in a date-rolled index; this is the cluster's retention
    POLICY and index template. It writes whatever the constant in the
    checked-out tree happens to say -- so a test run from any branch silently
    republishes that branch's retention settings over production's, and #186
    proposes making the policy body depend on a RETENTION_DAYS env var, at
    which point a test run would rewrite production retention to whatever the
    runner's environment happened to hold.

    Mock clients rather than enabled=False, because PipelineRunManager has no
    such flag and its methods dereference self.redis/self.es unconditionally.
    Configured to read as an EMPTY deployment (no active runs) rather than as
    bare MagicMocks: a MagicMock redis.get() returns a truthy Mock that the
    manager then tries to json.loads(). Empty is also closer to what a test
    should see than what it saw before -- which was the live deployment's real
    pipeline runs.

    Tests that exercise PipelineRunManager itself are unaffected: they either
    patch get_pipeline_run_manager (most of tests/unit/), patch the Redis and
    Elasticsearch classes in the module (tests/integration/
    test_pipeline_run_completion.py), or pass their own clients to the
    constructor.
    """
    from unittest.mock import MagicMock
    import services.pipeline_run as pipeline_run

    es = MagicMock()
    es.search.return_value = {'hits': {'total': {'value': 0}, 'hits': []}}
    redis_client = MagicMock()
    redis_client.get.return_value = None
    redis_client.hget.return_value = None
    redis_client.hgetall.return_value = {}
    redis_client.exists.return_value = 0

    pipeline_run._pipeline_run_manager = pipeline_run.PipelineRunManager(
        redis_client=redis_client,
        elasticsearch_client=es,
    )


# Every guard below installs at IMPORT time, not from pytest_configure, and that
# ordering is load-bearing (found in review). pytest_configure is a hook on this
# module, so by the time it fires this module's body has already run --
# including the two test-utility imports just below, which reach
# services.review_cycle, whose module scope constructs ReviewCycleExecutor() and
# so calls get_observability_manager() at import time, building both a Redis and
# an Elasticsearch client. Installed from pytest_configure the guards arrived
# one full unbounded resolver+retry round trip too late: `import
# tests.utils.builders` alone measured 5.0s on a host, logging "Failed to
# connect to Redis for observability: Error -3 connecting to redis:6379" before
# any guard existed. What they have to beat is this module's own first-party
# imports, not the first test module -- and nothing above this block imports
# first-party code, only socket/redis/elasticsearch/tempfile.
def _stop_the_background_call_trace_summarizer():
    """Keep GitHubAPIClient's housekeeping thread out of the test process (#186).

    `GitHubAPIClient.__init__` starts a `while True: sleep(300)` daemon thread
    per instance to summarize and trim its call-trace buffer. Production builds
    one client, so one thread. The suite builds one per fixture, and every one
    of them outlives its test and goes on mutating shared state inside every
    later test.

    Patched here rather than per file because nearly every test file that
    constructs a client forgets it. When this was written, five of the six such
    files did -- test_github_api_rate_limit_redis_mirror.py alone leaked 11
    threads, one per test using its `client` fixture -- and exactly one,
    test_github_app_rate_limit_accounting.py, remembered, at every one of its
    construction sites.

    Deliberately not restating that ratio as a number to maintain: it went
    stale within a day, when an unrelated PR added a sixth file. The argument
    does not depend on the count anyway -- this is a property of the
    constructor, so the constructor's own test harness is the only place that
    cannot be forgotten.

    Nothing under test depends on it: it does nothing at all within a 5-minute
    window and no test runs that long. Note that the method it would call,
    _summarize_and_cleanup_call_traces(), has NO test coverage either way -- a
    repo-wide grep finds no caller outside the production module. Neutralising
    the thread does not reduce coverage, because there is none to reduce.
    """
    try:
        from services.github_api_client import GitHubAPIClient
    except Exception as e:  # pragma: no cover - import shape, not behaviour
        logger.debug(f"Could not neutralise the call-trace summarizer: {e}")
        return

    GitHubAPIClient._start_call_trace_summarizer = lambda self: None


def _zero_the_agent_retry_backoff():
    """Stop the suite paying AgentExecutor's real retry backoff (#186 family).

    execute_agent() retries an ordinary agent failure twice, sleeping
    RETRY_BACKOFF_BASE_SECONDS * attempt between attempts -- 15s then 30s. A
    unit test whose agent raises a plain Exception matches none of the five
    retry exemptions (CancellationError, NonRetryableAgentError, a lock
    timeout, ClaudeCodeRateLimitError, breaker-open), so it takes the full
    path: 45 seconds of real asyncio.sleep() for a test that asserts on an
    emitted event. That was 45s of a 163s unit suite, the slowest test in it by
    two orders of magnitude, and every future test of the failure path would
    have paid it again without noticing.

    Only the WAIT is removed. attempt counting, the exemptions and the
    re-raise are untouched, so the tests that assert on retry behaviour --
    test_agent_executor_lock_timeout_no_retry.py counts
    run_with_circuit_breaker calls -- see exactly what they saw before. That
    file already stubbed asyncio.sleep per-test for this reason; this makes
    the stub unnecessary rather than contradicting it.

    Nothing under test depends on the delay itself. Its stated purpose is to
    let the Claude Code circuit breaker reach HALF_OPEN between attempts, and
    no unit test runs a real breaker across a real 30s recovery window.

    Assigning the module global works because the retry loop reads it on every
    use rather than binding it at import; test_agent_retry_backoff.py pins
    both halves of that -- the production default, and that this guard applied.

    Installed LAST of the guards, and that position is load-bearing for the
    same reason the block's ordering comment gives: this is the only guard
    whose import reaches services.agent_executor, and so transitively
    pipeline.factory and services.review_cycle, whose module scope builds
    Redis and Elasticsearch clients. Run before the observability and
    pipeline-run singletons are installed, it would pay for and pollute the
    live ones.
    """
    try:
        import services.agent_executor as agent_executor
    except Exception as e:  # pragma: no cover - import shape, not behaviour
        logger.debug(f"Could not zero the agent retry backoff: {e}")
        return

    agent_executor.RETRY_BACKOFF_BASE_SECONDS = 0


_refuse_to_resolve_compose_service_hostnames()
_bound_service_client_timeouts()
_redirect_orchestrator_root_to_scratch()
_clear_deployment_tuning_env_vars()
_install_a_disabled_observability_singleton()
_stop_the_background_call_trace_summarizer()
_install_a_mock_backed_pipeline_run_manager_singleton()
_zero_the_agent_retry_backoff()


# Import test utilities
from tests.mocks.github_mock import MockGitHubApp, MockGitHubIntegration, MockAgentExecutor
from tests.utils.builders import ReviewCycleStateBuilder, DiscussionBuilder, TaskContextBuilder


# ============================================================================
# Pytest Configuration
# ============================================================================

def pytest_configure(config):
    """Register custom markers and load environment.

    Deliberately NOT where the #174 service guards install -- see the comment
    above this module's own first-party imports for why they cannot wait this
    long. Marker registration genuinely belongs here.
    """
    config.addinivalue_line(
        "markers", "unit: Unit tests (fast, isolated)"
    )
    config.addinivalue_line(
        "markers", "integration: Integration tests (medium speed, real services)"
    )
    config.addinivalue_line(
        "markers", "e2e: End-to-end tests (slow, full system)"
    )
    config.addinivalue_line(
        "markers", "slow: Slow tests (skip in fast test runs)"
    )

    # Load environment variables from .env file for integration tests
    # This ensures API keys and other config are available
    from config.environment import Environment
    try:
        env = Environment()
        # Export environment variables if they're configured
        if env.claude_code_oauth_token:
            os.environ['CLAUDE_CODE_OAUTH_TOKEN'] = env.claude_code_oauth_token.get_secret_value()
        if env.anthropic_api_key:
            os.environ['ANTHROPIC_API_KEY'] = env.anthropic_api_key.get_secret_value()
        if env.github_token:
            os.environ['GITHUB_TOKEN'] = env.github_token.get_secret_value()
    except Exception as e:
        # Don't fail tests if .env is missing - some tests don't need it
        pass


# ============================================================================
# Cross-file sys.modules leakage (#133)
# ============================================================================

# The packages a test file replacing an entry for would corrupt every later
# test file in the session. Deliberately first-party only: mocking an optional
# third-party import out at module scope is a legitimate thing for a test to
# do, and this codebase's own modules are where the damage lands.
FIRST_PARTY_PACKAGES = (
    'agents', 'claude', 'config', 'monitoring', 'pipeline', 'services',
    'state_management', 'task_queue', 'utils',
)

# Filled in at collection finish, read by
# tests/unit/test_no_cross_file_module_leakage.py. A list rather than an
# assertion here so the failure arrives as an ordinary test failure with a
# traceback, instead of aborting collection for the whole run.
leaked_module_mocks = []


def _first_party_modules_replaced_by_mocks():
    """Names under FIRST_PARTY_PACKAGES whose sys.modules entry is a mock.

    Module-scope code runs when pytest imports a test file, which happens
    during collection -- so by the time collection finishes, every
    `sys.modules['services.x'] = MagicMock()` written at a test module's top
    level is already in place and will stay there for the rest of the session.
    """
    import sys
    from unittest.mock import NonCallableMock

    leaked = []
    for name, module in list(sys.modules.items()):
        if not name.startswith(FIRST_PARTY_PACKAGES):
            continue
        if isinstance(module, NonCallableMock) or type(module).__name__ in (
            'MagicMock', 'Mock', 'AsyncMock'
        ):
            leaked.append(name)
    return sorted(leaked)


def pytest_collection_finish(session):
    """
    Record first-party modules that a test file mocked out permanently (#133).

    tests/unit/test_pr_review_phase_recovery.py and two others assigned
    MagicMocks into sys.modules at module scope and never removed them, because
    services.dev_container_state and services.work_execution_state build a
    singleton at import time under a state directory that does not exist off
    -container. The assignment outlived the file that made it:
    claude/docker_runner.py's _get_image_for_agent() imports
    dev_container_state from that same entry, so _build_docker_command()
    appended a MagicMock as the image name and
    tests/unit/test_docker_runner_worktree_mount.py's ' '.join(cmd) raised
    "expected str instance, MagicMock found" -- but only when the two files ran
    in the same session, in that order. That is a large part of why the suite's
    result depended on how it was chunked.

    The root cause is fixed at source (see
    _redirect_orchestrator_root_to_scratch), so this exists to keep it
    fixed: any new file that reaches for the same workaround shows up as one
    named test failure rather than as somebody else's inexplicable TypeError.
    """
    leaked_module_mocks[:] = _first_party_modules_replaced_by_mocks()


def pytest_sessionfinish(session, exitstatus):
    """
    Catch the same leak inserted at RUN time rather than at import time (#133).

    The snapshot above only sees module-scope assignments, because that is when
    pytest imports a test file -- and that is not the only shape the workaround
    took. On main, tests/unit/test_docker_runner_validation.py did the
    assignment inside a helper method:

        def _get_non_retryable_class(self):
            if 'services.dev_container_state' not in sys.modules:
                sys.modules['services.dev_container_state'] = MagicMock()

    which runs long after pytest_collection_finish has taken its sample. A
    fixture, a setUp or a helper is the natural place for the next one now that
    the module-scope form is visibly discouraged, and a leak there would be
    invisible to a test asserting on a list captured before it happened.

    Reported here rather than as a named test failure because there is no test
    left to fail by this point -- but it still fails the run, because a session
    that ends with claude.docker_runner's dev_container_state replaced by a
    MagicMock has not tested what its green line says it did.
    """
    late = [
        name for name in _first_party_modules_replaced_by_mocks()
        if name not in leaked_module_mocks
    ]
    if not late:
        return

    reporter = session.config.pluginmanager.get_plugin('terminalreporter')
    if reporter is not None:
        reporter.write_sep('=', "first-party modules left mocked in sys.modules", red=True)
        reporter.write_line(
            f"{late} were replaced by mocks WHILE TESTS RAN and never put back. "
            "Every test that imported one of them afterwards got the mock, "
            "whichever file it belongs to -- which is how the same suite produces "
            "different results depending on how it is chunked. Use a "
            "fixture-scoped patch, or fix what makes the real import fail."
        )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ============================================================================
# Container-gated test reporting
# ============================================================================

# The reason string ~60 test files pass to pytest.skip(..., allow_module_level=True)
# when /app is absent. They import agents/__init__.py and other modules that
# genuinely cannot import outside the orchestrator container (see CLAUDE.md,
# "Docker-only imports"), so the gate itself is correct. What they test for is
# ORCHESTRATOR_CONTAINER_MARKER / running_in_orchestrator_container(), which
# live at the top of this file because the #174 guards run before anything else.
CONTAINER_ONLY_SKIP_REASON = "Requires Docker container environment"


def pytest_report_header(config):
    """
    Say up front whether the container-gated portion of the suite can run at all
    (#140 item 37).

    A host run skips those files wholesale, and pytest's summary line reports
    those skips indistinguishably from any other -- so the run looks green while
    a large share of it never executed. It is now stated before the first test.
    """
    if running_in_orchestrator_container():
        return f"orchestrator container: yes ({ORCHESTRATOR_CONTAINER_MARKER} present)"
    return (
        f"orchestrator container: NO ({ORCHESTRATOR_CONTAINER_MARKER} absent) -- every "
        "container-gated test file will be SKIPPED, not run. For full coverage: "
        "docker exec -w /workspace/switchyard switchyard-orchestrator-1 python -m pytest <path>"
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """
    Count the container-gated files that never ran, at the bottom of the report
    where the green/red verdict is (#140 item 37).

    Deliberately reporting, not failing: a host run of a scoped subset is a
    legitimate thing to do, and turning it into an error would just teach people
    to pass -p no:cacheprovider-style opt-outs. What it must not do is look like
    a clean full pass.
    """
    if running_in_orchestrator_container():
        return

    gated = [
        report for report in terminalreporter.stats.get('skipped', [])
        if CONTAINER_ONLY_SKIP_REASON in str(getattr(report, 'longrepr', ''))
    ]
    if not gated:
        return

    terminalreporter.write_sep(
        '=', f"{len(gated)} container-gated test file(s) did NOT run", red=True
    )
    terminalreporter.write_line(
        f"These skipped because {ORCHESTRATOR_CONTAINER_MARKER} is absent, not because they "
        "passed. This result does not cover them."
    )
    terminalreporter.write_line(
        "Re-run inside the orchestrator container: "
        "docker exec -w /workspace/switchyard switchyard-orchestrator-1 python -m pytest <path>"
    )


# ============================================================================
# Project config isolation
# ============================================================================

# Tracked home of the suite's fake project configs. See #140 items 35/38.
FIXTURE_PROJECTS_DIR = Path(__file__).parent / 'fixtures' / 'config' / 'projects'


def build_project_config_overlay(overlay_dir: Path, fixture_dir: Path,
                                 deployment_dir: Path) -> Path:
    """
    Populate `overlay_dir` with a symlink per project config: every fixture in
    `fixture_dir` first, then every real config in `deployment_dir` that a
    fixture has not already claimed by name.

    ADDITIVE, deliberately (#154/WI-9 review). Replacing projects_dir outright
    made the fixture a silent, global, opt-out-less redirect: any test that asks
    ConfigManager for a real deployment project by name -- e.g.
    tests/integration/test_readonly_filesystem.py's
    get_project_agent_config('context-studio', ...) -- got a FileNotFoundError
    naming a path under tests/fixtures/, with nothing to suggest a session
    fixture had moved the directory out from under it. Overlaying keeps the
    fixtures reachable without taking the real ones away.

    Fixtures shadow same-named deployment files rather than the reverse: a stray
    config/projects/test_project.yaml left behind in a deployment (exactly what
    #162 is about) must not be what the suite reads.

    Snapshot, not a live view: a config written into `deployment_dir` after this
    runs is not picked up. Nothing in the suite does that, and a live view would
    need a projects_dir shim rather than a real directory.
    """
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for source_dir in (fixture_dir, deployment_dir):
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.glob('*.yaml')):
            target = overlay_dir / source.name
            if target.exists() or target.is_symlink():
                continue
            target.symlink_to(source.resolve())
    return overlay_dir


@pytest.fixture(scope="session", autouse=True)
def isolated_project_configs(tmp_path_factory):
    """
    Point the process-wide ConfigManager at a session overlay of
    tests/fixtures/config/projects/ over config/projects/ (#140 items 35/38).

    Two problems, one root cause. `config/projects/` is gitignored AS A
    DIRECTORY, so the `test_project.yaml` / `test-project.yaml` fixtures several
    test files need could not be committed there -- every fresh checkout and
    every new worktree failed those tests until somebody hand-copied the files
    in. And because that directory is also the REAL deployment's project config
    directory, the copies that did exist were loaded by the running orchestrator
    as ordinary projects: a 166-day-old stale `test-project/planning` pipeline
    lock re-evaluated on every startup, board reconciliation and workspace init
    for a project that does not exist, and real dev_environment_setup /
    dev_environment_verifier agent runs dispatched against
    /workspace/test-project -- burning tokens and container slots, with their
    output addressed to issue #0 (see #162, and #149's FAILSAFE branch guard,
    which is what finally made those runs visible by refusing to commit them).

    The same file cannot be both test input that must exist and deployment
    config that must not. So the fixtures live here, tracked, and this fixture
    redirects lookups at them; `config/projects/` is left to real projects only.

    Session-scoped and autouse rather than opt-in: the tests that need it reach
    config_manager indirectly (PipelineQueueManager._get_pipeline_trigger_column()
    -> config_manager.get_project_config(self.project_name)), so there is no
    call site to opt in at, and a test that forgot to would silently read the
    deployment's real projects instead.

    Autouse also means it applies to tests that never asked for it, which is why
    it OVERLAYS rather than replaces -- see build_project_config_overlay(). The
    real configs stay reachable by name; only the two fixture projects are added.

    Only the singleton is redirected. A test constructing its own
    ConfigManager() still gets `config/projects/` directly, which on a clean
    checkout is empty -- the same answer for the fixture projects, since both are
    `hidden: true` and so never appear in list_visible_projects() either way.
    """
    from config.manager import config_manager

    original = config_manager.projects_dir
    overlay = build_project_config_overlay(
        tmp_path_factory.mktemp('project-configs'), FIXTURE_PROJECTS_DIR, original
    )
    config_manager.projects_dir = overlay
    config_manager.reload_config()
    try:
        yield overlay
    finally:
        config_manager.projects_dir = original
        config_manager.reload_config()


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Path Fixtures
# ============================================================================

@pytest.fixture
def tests_dir():
    """Path to tests directory"""
    return Path(__file__).parent


@pytest.fixture
def fixtures_dir(tests_dir):
    """Path to fixtures directory"""
    return tests_dir / 'fixtures'


@pytest.fixture
def discussions_fixtures_dir(fixtures_dir):
    """Path to discussion fixtures"""
    return fixtures_dir / 'discussions'


# ============================================================================
# Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_github_app():
    """Create a MockGitHubApp instance"""
    app = MockGitHubApp()
    yield app
    app.reset()


@pytest.fixture
def mock_github_integration(mock_github_app):
    """Create a MockGitHubIntegration instance"""
    return MockGitHubIntegration(mock_github_app)


@pytest.fixture
def mock_agent_executor():
    """Create a MockAgentExecutor instance"""
    executor = MockAgentExecutor()
    yield executor
    executor.reset()


@pytest.fixture
def patch_github_api(monkeypatch, mock_github_app):
    """
    Patch GitHub API to use mock

    Usage:
        def test_something(patch_github_api):
            # GitHub API calls will use mock
            ...
    """
    from services.github_app import github_app
    monkeypatch.setattr(github_app, 'graphql_request', mock_github_app.graphql_request)
    monkeypatch.setattr(github_app, 'rest_request', mock_github_app.rest_request)
    monkeypatch.setattr(github_app, 'get_installation_token', mock_github_app.get_installation_token)

    return mock_github_app


# ============================================================================
# Builder Fixtures
# ============================================================================

@pytest.fixture
def review_cycle_builder():
    """Create a ReviewCycleStateBuilder"""
    return ReviewCycleStateBuilder()


@pytest.fixture
def discussion_builder():
    """Create a DiscussionBuilder"""
    return DiscussionBuilder()


@pytest.fixture
def task_context_builder():
    """Create a TaskContextBuilder"""
    return TaskContextBuilder()


# ============================================================================
# Common Test Data Fixtures
# ============================================================================

@pytest.fixture
def sample_issue_data():
    """Common issue data structure"""
    return {
        'number': 96,
        'title': 'Test Feature',
        'body': 'Test feature description',
        'state': 'open',
        'labels': []
    }


@pytest.fixture
def sample_ba_output():
    """Sample business analyst output"""
    return """## Business Requirements Analysis

**Feature**: Test Feature

## Functional Requirements

FR-1: The system shall do X
FR-2: The system shall do Y

## User Stories

US-1: As a user, I want to X

_Processed by the business_analyst agent_"""


@pytest.fixture
def sample_reviewer_feedback():
    """Sample requirements reviewer feedback"""
    return """## Review of Business Analysis

**Status**: Changes Requested

## Issues Found

### High Severity
- FR-1 lacks acceptance criteria

### Medium Severity
- US-1 needs more detail

_Processed by the requirements_reviewer agent_"""


@pytest.fixture
def sample_ba_revision():
    """Sample business analyst revision"""
    return """## Revision Notes
- Added acceptance criteria to FR-1
- Expanded US-1 with more detail

## Business Requirements Analysis (Revised)

FR-1: The system shall do X
  **Acceptance Criteria**: Given X, when Y, then Z

US-1: As a user, I want to X so that Y
  **Acceptance Criteria**:
  - Given A, when B, then C

_Processed by the business_analyst agent_"""


@pytest.fixture
def sample_reviewer_approval():
    """Sample requirements reviewer approval"""
    return """## Review Complete

All requirements have been addressed. The business analysis is comprehensive and ready to proceed.

**Status**: APPROVED

## Assessment

All acceptance criteria defined
User stories follow INVEST principles
Requirements are clear and testable

_Processed by the requirements_reviewer agent_"""


@pytest.fixture
def simple_discussion(discussion_builder, sample_ba_output, sample_reviewer_feedback):
    """
    Simple discussion with 1 iteration:
    - BA initial output
    - Reviewer feedback
    """
    return (discussion_builder
        .with_id('D_test_simple')
        .with_number(1)
        .with_title('Simple Test Discussion')
        .with_comment('orchestrator-bot', sample_ba_output, is_ba=True)
        .with_comment('orchestrator-bot', sample_reviewer_feedback, is_reviewer=True)
        .build())


@pytest.fixture
def discussion_with_human_feedback(discussion_builder, sample_ba_output):
    """
    Discussion with BA output and human question
    """
    return (discussion_builder
        .with_id('D_test_feedback')
        .with_number(2)
        .with_title('Discussion with Feedback')
        .with_comment('orchestrator-bot', sample_ba_output, is_ba=True)
        .with_reply('tinkermonkey', 'Can you clarify requirement FR-1?', to_comment=0)
        .build())


@pytest.fixture
def multi_iteration_discussion(
    discussion_builder,
    sample_ba_output,
    sample_reviewer_feedback,
    sample_ba_revision
):
    """
    Discussion with 2 complete iterations:
    - BA initial → RR review → BA revision → RR review 2
    """
    return (discussion_builder
        .with_id('D_test_multi')
        .with_number(3)
        .with_title('Multi-Iteration Discussion')
        .with_comment('orchestrator-bot', sample_ba_output, is_ba=True)
        .with_comment('orchestrator-bot', sample_reviewer_feedback, is_reviewer=True)
        .with_comment('orchestrator-bot', sample_ba_revision, is_ba=True)
        .with_comment('orchestrator-bot', sample_reviewer_feedback, is_reviewer=True)
        .build())


# ============================================================================
# State Fixtures
# ============================================================================

@pytest.fixture
def initial_review_cycle_state(review_cycle_builder):
    """Review cycle state at initialization"""
    return (review_cycle_builder
        .for_issue(96)
        .in_repository('context-studio')
        .with_agents('business_analyst', 'requirements_reviewer')
        .for_project('context-studio', 'idea-development')
        .in_discussion('D_test123')
        .initialized()
        .build())


@pytest.fixture
def escalated_review_cycle_state(review_cycle_builder, sample_ba_output, sample_reviewer_feedback):
    """Review cycle state that has been escalated"""
    return (review_cycle_builder
        .for_issue(96)
        .in_repository('context-studio')
        .with_agents('business_analyst', 'requirements_reviewer')
        .for_project('context-studio', 'idea-development')
        .in_discussion('D_test123')
        .at_iteration(3)
        .with_maker_output(sample_ba_output, iteration=0)
        .with_review_output(sample_reviewer_feedback, iteration=1)
        .with_maker_output(sample_ba_output, iteration=2)
        .with_review_output(sample_reviewer_feedback, iteration=3)
        .escalated()
        .build())


# ============================================================================
# Fixture Loader
# ============================================================================

@pytest.fixture
def load_discussion_fixture(discussions_fixtures_dir):
    """
    Helper to load discussion fixtures from JSON files

    Usage:
        def test_something(load_discussion_fixture):
            discussion = load_discussion_fixture('discussion_95.json')
    """
    import json

    def _load(filename: str) -> Dict[str, Any]:
        filepath = discussions_fixtures_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Fixture not found: {filepath}")

        with open(filepath) as f:
            data = json.load(f)

        # Extract discussion node from repository wrapper if present
        if 'repository' in data and 'discussion' in data['repository']:
            return data['repository']['discussion']
        elif 'node' in data:
            return data['node']
        else:
            return data

    return _load


# ============================================================================
# Async Test Helpers
# ============================================================================

@pytest.fixture
def async_return():
    """
    Helper to create async functions that return a value

    Usage:
        mock_fn = async_return({'result': 'success'})
        result = await mock_fn()
    """
    def _create_async(value):
        async def _async_fn(*args, **kwargs):
            return value
        return _async_fn
    return _create_async


# ============================================================================
# Test Data Cleanup
# ============================================================================

# All project names used exclusively in tests — safe to purge completely.
_TEST_PROJECT_NAMES = ["test-project", "test_project", "test-proj"]

# ES indices that carry a top-level `project` field written by tests.
_TEST_ES_INDICES = [
    "pipeline-runs-*",
    "decision-events-*",
    "agent-events-*",
    "orchestrator-test-cycle-records",
    "orchestrator-task-metrics-*",
    "orchestrator-quality-metrics-*",
]


def _may_purge_service_data() -> bool:
    """True only when the Redis/ES this run can reach are ones it owns.

    Found in review: this used to rely on the services simply being
    unreachable off-container, which is not a property the suite controls. The
    developer machine a host run is meant to help is also the one running
    docker-compose, which publishes 6379 and 9200 on the host — so "the ping
    succeeded" was never licence to DEL. Inside the orchestrator container the
    suite genuinely owns both stores, and a runner that deliberately supplied
    them says so with SWITCHYARD_TEST_ALLOW_SERVICE_HOSTS=1.
    """
    return running_in_orchestrator_container() or ALLOW_REAL_SERVICE_HOSTS


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data():
    """
    Purge Elasticsearch and Redis data belonging to test-only projects.

    Runs once before and once after the entire test session so leftover data
    from a previous crashed run is also removed. Unit tests that mock ES/Redis
    are unaffected, and a run that does not own a Redis/ES does not purge at
    all — see _may_purge_service_data().
    """
    if not _may_purge_service_data():
        yield
        return

    _purge_test_data()
    yield
    _purge_test_data()


def _purge_test_data():
    _purge_elasticsearch()
    _purge_redis()


def _purge_elasticsearch():
    try:
        import os
        from elasticsearch import Elasticsearch
        es_url = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
        es = Elasticsearch(es_url, request_timeout=5)
        if not es.ping():
            return
        query = {"query": {"terms": {"project": _TEST_PROJECT_NAMES}}}
        for index in _TEST_ES_INDICES:
            try:
                es.delete_by_query(
                    index=index,
                    body=query,
                    ignore_unavailable=True,
                    refresh=True,
                )
            except Exception as e:
                # Logged, not swallowed silently (found in review): this issues
                # delete_by_query, and a purge that fires against something
                # other than the store it meant to must leave a trace.
                logger.warning(f"Test-data purge: delete_by_query on {index} failed: {e}")
    except Exception as e:
        logger.warning(f"Test-data purge: Elasticsearch cleanup skipped: {e}")


def _purge_redis():
    try:
        import os
        import redis as redis_lib
        from urllib.parse import urlparse
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379")
        parsed = urlparse(redis_url)
        r = redis_lib.Redis(host=parsed.hostname, port=parsed.port or 6379, socket_timeout=2)
        r.ping()
        for project in _TEST_PROJECT_NAMES:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=f"*{project}*", count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break

        # GitHubAPIClient mirrors real-response-derived rate limit readings
        # to a couple of small *global* Redis keys (not namespaced by
        # project, since GitHub's quota is account-wide) that the live
        # dashboard reads directly - see get_shared_rate_limit_status().
        # A unit test that exercises graphql()/rest()/http_request() with a
        # mocked subprocess/response still runs the real mirror code, which
        # would otherwise leave fabricated numbers sitting in the same keys
        # production reads from. Purge them explicitly since they don't
        # match the project-name pattern above.
        try:
            from services.github_api_client import RATE_LIMIT_REDIS_KEYS
            r.delete(*RATE_LIMIT_REDIS_KEYS.values())
        except Exception as e:
            logger.warning(f"Test-data purge: rate-limit key cleanup failed: {e}")
    except Exception as e:
        logger.warning(f"Test-data purge: Redis cleanup skipped: {e}")


# ============================================================================
# Leaked-thread detection (#186)
# ============================================================================

# How long a thread started during a test may take to finish after it ends.
# Generous on purpose: the point is to catch threads that run FOREVER, not to
# police a slow teardown.
LEAKED_THREAD_GRACE_SECONDS = float(
    os.environ.get('SWITCHYARD_TEST_THREAD_GRACE', '2.0')
)

# Worker threads of executor pools that are not any one test's leak. The two
# entries are exempt for DIFFERENT reasons, which the first version of this
# comment got wrong by lumping them together:
#
#   * 'epic-worktree' -- a genuine module-global, lazily built and guarded
#     (services/project_workspace.py:99-108), living for the rest of the
#     session by design. Whichever test touches it first appears to start its
#     workers. It exists so that a lock wait does not run on the caller's
#     thread (#151/WI-6).
#   * 'project-init' -- NOT global and NOT lazy. It is a `with
#     ThreadPoolExecutor(...)` block local to initialize_all_projects()
#     (services/project_workspace.py:492-495), so `with` joins its workers
#     before the call returns and they cannot outlive the grace window anyway.
#     It exists to stop startup taking len(projects) x 120s (#140 item 3), not
#     for lock waits. Listed defensively; if it ever trips this guard,
#     something is wrong with the pool rather than with the test.
PERSISTENT_POOL_THREAD_PREFIXES = ('epic-worktree', 'project-init')


@pytest.fixture(autouse=True)
def _fail_on_leaked_threads(request):
    """Fail a test that leaves a thread of its own still running.

    `_start_review_cycle_for_issue` ends by spawning a daemon thread that runs
    a real review cycle. Two test files drove it past its pipeline-lock gate
    and never joined the thread, so the cycle kept running into whatever test
    came next -- connecting to Redis, indexing to Elasticsearch, and taking
    file locks under a RELATIVE `state/projects/<project>/...` path while
    unrelated tests were asserting. It surfaced only as a stray log line inside
    an unrelated test's captured output, and its timing was nondeterministic --
    precisely the shape that makes a suite's result depend on how it was
    chunked (#186, #133, #180).

    Daemon threads are both the dangerous ones and the easy ones to miss:
    nothing joins them and the process exits regardless, so without this the
    only symptom is somebody else's inexplicable failure weeks later. Adding
    this guard immediately surfaced a second, unrelated leak nobody had filed
    -- GitHubAPIClient's call-trace summarizer, 11 threads from one file.

    Opt out with `@pytest.mark.allow_thread_leak` for a test that deliberately
    leaves something running. There are none today, and a new one should have
    to say so out loud.
    """
    if request.node.get_closest_marker('allow_thread_leak'):
        yield
        return

    import threading
    # Thread OBJECTS, not idents. CPython recycles Thread.ident aggressively --
    # 50 sequential short-lived threads measured as ONE distinct ident -- so an
    # ident-based snapshot lets a new leak inherit a dead thread's number and
    # go unseen. enumerate() only ever returns live threads, and Thread hashes
    # by identity, so this is exact.
    before = set(threading.enumerate())

    yield

    deadline = time.monotonic() + LEAKED_THREAD_GRACE_SECONDS
    while True:
        leaked = [
            t for t in threading.enumerate()
            if t not in before
            and t.is_alive()
            and not t.name.startswith(PERSISTENT_POOL_THREAD_PREFIXES)
        ]
        if not leaked or time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    if leaked:
        described = ', '.join(f"{t.name}(daemon={t.daemon})" for t in leaked)
        pytest.fail(
            f"{len(leaked)} thread(s) started by this test were still running "
            f"{LEAKED_THREAD_GRACE_SECONDS}s after it finished: {described}. "
            f"A thread that outlives its test does real work inside later "
            f"tests -- see #186. Join it, patch it out (see "
            f"tests.utils.builders.RecordedThread), or mark the test "
            f"@pytest.mark.allow_thread_leak if the leak is deliberate."
        )


# ============================================================================
# Process-global restoration (#181, #186, #133)
# ============================================================================

# Package prefixes whose modules hold singletons built at import time. A test
# that pops one from sys.modules to force a re-import replaces those singletons
# -- and the class objects -- for the rest of the session.
FIRST_PARTY_PREFIXES = (
    'services.', 'config.', 'pipeline.', 'claude.', 'monitoring.',
    'state_management.', 'task_queue.', 'agents.',
)

# Modules a test is allowed to importlib.reload(). Reload is not restorable --
# see _restore_process_globals below -- so what this list buys is that a NEW
# one cannot be added silently.
#
# One entry, and the detector is what identified it: #203 predicted
# `services.data_retention`, because that is the name on the test file. The
# module actually reloaded is `config.retention`, at eight sites in
# tests/unit/services/test_data_retention.py (four tests, each reloading once
# under a patched RETENTION_DAYS and once more in a `finally`).
#
# Allowed because config/retention.py defines no class and no object: four
# module-level ints and five functions. So a reload mints nothing that
# `isinstance` or `patch(...)` can start disagreeing about. What it DOES leave
# stale is the copy each `from config.retention import RETENTION_DAYS` took --
# thirteen non-test modules do that, observability.py and pipeline_run.py among
# them -- which is why those four tests restore the module in a `finally`
# rather than leaving the last reload standing.
RELOADABLE_FIRST_PARTY_MODULES = frozenset({
    'config.retention',
})


@pytest.fixture(autouse=True)
def _restore_process_globals():
    """Undo the two process-wide mutations tests reach for to isolate themselves.

    Both are the same instinct -- "point the state modules at my tmp_path" --
    and both outlive the test that did it:

      * `os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)`. Assigned directly
        rather than through monkeypatch, so it survives. Every later test that
        resolves a state path gets a directory pytest has since deleted. Two
        files do this (test_work_execution_redis_recovery.py,
        test_stale_execution_history.py, the latter at six separate sites).
      * `sys.modules.pop('services.work_execution_state', None)` to force a
        re-import. The re-import builds a NEW module-level singleton and a NEW
        class object, so anything holding the old one keeps a stale root and
        `isinstance` against the old class starts returning False.

    Caught by test_state_root_isolation.py's singleton check, which failed in
    full-suite order while passing alone -- the signature of exactly the
    contamination this suite keeps paying for.

    Restores rather than forbids: these tests are doing something reasonable,
    they just need it undone. Only entries that were REPLACED or REMOVED are
    put back, so modules a test legitimately imports for the first time stay.

    THIRD, `importlib.reload` -- DETECTED here, not restored, and the
    difference is deliberate (#203).

    Reload re-executes the module body in place and keeps the same object, so
    `sys.modules.get(name) is module` stays True and the loop below never
    fires, even though the reload has rebuilt every module-level singleton and
    minted a new class object. The per-module sentinel that does see it is
    `module.__spec__`: importlib._bootstrap._exec assigns a freshly-found spec
    on every reload, so spec IDENTITY changes while module identity does not.
    Measured on this container's Python 3.11.16: after `importlib.reload(m)`,
    `m.__spec__` is a different object, `m.__dict__` is the SAME object, and an
    attribute stamped into `m.__dict__` beforehand survives -- which is why a
    stamped counter cannot be the sentinel and the spec can.

    Restoring is not on the table. There is nothing to put back: the damaged
    object IS the live one, and the only way to rebuild it is another reload,
    which mints yet another class object and does the same damage again. So it
    raises instead. Being a teardown, pytest reports that as an ERROR against
    the offending test rather than a FAILURE -- the test body itself still
    passes -- which is loud, non-zero, and lands next to its cause. Modules on
    RELOADABLE_FIRST_PARTY_MODULES above are allowed.

    LIMIT, stated so the docstring does not outrun the code: a module that a
    test imports for the FIRST time and then reloads within the same test is
    not in `previous_modules`, so its reload is not seen. Nothing in the suite
    does that today.
    """
    from unittest.mock import NonCallableMock

    previous_root = os.environ.get('ORCHESTRATOR_ROOT')
    previous_modules = {
        name: module for name, module in sys.modules.items()
        if name.startswith(FIRST_PARTY_PREFIXES)
    }
    # Strong references, not ids: the old spec is dropped on reload and a new
    # object can land at the same address.
    previous_specs = {
        name: getattr(module, '__spec__', None)
        for name, module in previous_modules.items()
    }

    yield

    if previous_root is None:
        os.environ.pop('ORCHESTRATOR_ROOT', None)
    elif os.environ.get('ORCHESTRATOR_ROOT') != previous_root:
        os.environ['ORCHESTRATOR_ROOT'] = previous_root

    for name, module in previous_modules.items():
        current = sys.modules.get(name)
        if current is module:
            continue
        if isinstance(current, NonCallableMock):
            # Leave it. pytest_sessionfinish above exists to FAIL the run when a
            # first-party module is left replaced by a mock (#133), and it reads
            # sys.modules to find out. Restoring here would put the module back
            # before that check runs, so the detector would see a clean session
            # and the leak it was written for would become undetectable --
            # verified: a test that assigns a MagicMock and never restores it
            # goes from "first-party modules left mocked in sys.modules" to
            # total silence with this fixture in place.
            #
            # So: repair an honest re-import, never a mock. Those are different
            # mistakes and only one of them is the caller's own business.
            continue
        sys.modules[name] = module

    reloaded = sorted(
        name for name, module in previous_modules.items()
        if name not in RELOADABLE_FIRST_PARTY_MODULES
        and sys.modules.get(name) is module
        and previous_specs[name] is not None
        and getattr(module, '__spec__', None) is not previous_specs[name]
    )
    assert not reloaded, (
        "this test called importlib.reload() on a first-party module: "
        + ', '.join(reloaded) +
        ". Reload re-executes the module body in place, so every module-level "
        "singleton is rebuilt and every class object is replaced while the "
        "module object -- and therefore every `import x` anyone already did -- "
        "stays the same. Nothing can put that back (#181, #203). Construct the "
        "object under a patched environment instead; see "
        "tests/unit/test_state_root_isolation.py::TestBothHoldoutsUseIt for the "
        "pattern. If the module genuinely has nothing process-wide to damage, "
        "add it to RELOADABLE_FIRST_PARTY_MODULES in this file with the reason."
    )
