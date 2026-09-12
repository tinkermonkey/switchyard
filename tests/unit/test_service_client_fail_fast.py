"""
A test run cannot block on a service that is not there (#174).

On a bare python:3.11-slim runner with only `pip install -r requirements.txt`,
`pytest tests/unit` did not finish: it reached services/cancellation.py's
`redis.Redis(host='redis', port=6379, decode_responses=True).ping()` and sat
there, ~6s of CPU over 3+ minutes at ~0.2%. Nothing bounded it: pytest.ini's
`timeout = 300` was dead config at the time -- wrong section, and pytest-timeout
not installed (#204).

An earlier version of this docstring gave the reason as "pytest-timeout cannot
interrupt a blocking socket call in the main thread". That is not true and was
never measured; see tests/conftest.py's
_refuse_to_resolve_compose_service_hostnames for the measurement that replaced
it. The live 180s timeout does bound this, but as a 180s abort-with-stacks; the
guards below keep it to a fast, specific failure instead.

The obvious reading is a missing socket_connect_timeout, and ~20 call sites
across services/, monitoring/, claude/ and task_queue/ do omit one. Timing it in
that container says otherwise:

    redis.Redis(host='redis', ...).ping()                       -> 5.04s
    redis.Redis(host='redis', ..., socket_connect_timeout=2)    -> 4.16s
    ...with the resolver taken out of the path, still           -> 4.51s
    ...with retry=Retry(NoBackoff(), 0)                         -> 0.02s

Two separate costs, neither of them the connect timeout:

  - `socket.getaddrinfo`, which no client-level timeout bounds. A bare name plus
    the runner's `search` suffixes is several queries against whatever
    nameservers it was handed, and how long they take to give up is a property
    of that machine -- which is why the same suite took seconds one hour and had
    not finished in 150 the next. tests/conftest.py refuses the three compose
    service names outright off-container, which takes the resolver out of the
    path entirely. It refuses rather than re-points them because the machine a
    host run is meant to help is also the one running docker-compose, which
    publishes 6379 and 9200 on the host -- see that guard's docstring.
  - redis-py 8's default client retry policy, Retry(ExponentialWithJitter
    Backoff(base=1, cap=10), retries=10). Even a connect refused instantly is
    retried eleven times with a growing sleep between them. tests/conftest.py
    defaults it to no retries and no backoff for the test run.

Both are needed; the fourth line above is what either one alone leaves behind.
"""

import contextlib
import os
import socket
import time
from unittest.mock import patch

import pytest

from tests.conftest import (
    COMPOSE_SERVICE_HOSTS,
    TEST_SERVICE_CONNECT_TIMEOUT,
    TEST_SERVICE_OP_TIMEOUT,
    _bound_service_client_timeouts,
    _redirect_orchestrator_root_to_scratch,
    _may_purge_service_data,
    _patch_init_defaults,
    _refuse_to_resolve_compose_service_hostnames,
)


@contextlib.contextmanager
def _guard_installed(in_container=False):
    """Install the name guard for the duration of a test and take it back out.

    socket.getaddrinfo is process-global, and the guard is deliberately
    idempotent, so a test that left it behind would silently change what every
    later test in the session resolves.
    """
    original = socket.getaddrinfo
    with patch('tests.conftest.running_in_orchestrator_container', return_value=in_container):
        try:
            _refuse_to_resolve_compose_service_hostnames()
            yield original
        finally:
            socket.getaddrinfo = original


class TestServiceHostnamesFailToResolveWithoutTheResolver:

    def test_it_names_the_services_this_codebase_connects_to_by_bare_hostname(self):
        for host in ('redis', 'elasticsearch'):
            assert host in COMPOSE_SERVICE_HOSTS

    def test_off_container_a_service_name_does_not_resolve_at_all(self):
        with _guard_installed():
            for host in COMPOSE_SERVICE_HOSTS:
                with pytest.raises(socket.gaierror):
                    socket.getaddrinfo(host, 6379)

    def test_it_never_hands_back_a_reachable_address(self):
        """The regression this replaced (found in review): resolving these to
        127.0.0.1 was safe only while nothing was listening there, and
        docker-compose.yml publishes redis as "6379:6379" and elasticsearch as
        "9200:9200" -- so on the machine a host run is meant to help, loopback
        IS the live deployment. A name that does not resolve cannot reach
        anything, whatever the developer happens to be running."""
        with _guard_installed():
            with pytest.raises(socket.gaierror):
                socket.getaddrinfo('redis', 6379)

    def test_the_refusal_is_an_oserror_so_clients_see_an_ordinary_outage(self):
        """redis-py wraps OSError from the connect into redis.ConnectionError
        and elastic_transport does the same, which is what every caller in this
        codebase already handles."""
        with _guard_installed():
            with pytest.raises(OSError):
                socket.getaddrinfo('redis', 6379)

    def test_it_leaves_every_other_hostname_to_the_real_resolver(self):
        with _guard_installed():
            with pytest.raises(socket.gaierror):
                socket.getaddrinfo('switchyard-no-such-host.invalid', 80)

    def test_inside_the_container_the_real_hosts_are_left_alone(self):
        """`redis` and `elasticsearch` ARE real there, and the suite uses
        them -- tests/conftest.py's own test-data purge, for one."""
        original = socket.getaddrinfo
        with patch('tests.conftest.running_in_orchestrator_container', return_value=True):
            _refuse_to_resolve_compose_service_hostnames()

        assert socket.getaddrinfo is original


class TestTheTestDataPurgeCannotRunAgainstSomebodyElsesStore:
    """
    Found in review. The session-autouse cleanup_test_data fixture SCANs and
    DELs test-project keys, unconditionally deletes the two global
    github:rate_limit:* keys the live dashboard reads, and issues
    delete_by_query across five index patterns. Its whole safety argument used
    to be that off-container the services are unreachable -- which is not a
    property the suite controls on a machine that is also running the stack.
    """

    def test_a_host_run_does_not_purge(self):
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False), \
                patch('tests.conftest.ALLOW_REAL_SERVICE_HOSTS', False):
            assert _may_purge_service_data() is False

    def test_the_orchestrator_container_owns_its_stores_and_does_purge(self):
        with patch('tests.conftest.running_in_orchestrator_container', return_value=True):
            assert _may_purge_service_data() is True

    def test_a_runner_that_supplied_real_service_containers_does_purge(self):
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False), \
                patch('tests.conftest.ALLOW_REAL_SERVICE_HOSTS', True):
            assert _may_purge_service_data() is True


class TestRedisConnectIsBounded:

    def test_a_client_built_with_no_timeouts_gets_them(self):
        """The exact shape services/cancellation.py uses."""
        import redis

        client = redis.Redis(host='nonexistent-host', port=6379, decode_responses=True)
        kwargs = client.connection_pool.connection_kwargs

        assert kwargs['socket_connect_timeout'] == TEST_SERVICE_CONNECT_TIMEOUT
        assert kwargs['socket_timeout'] == TEST_SERVICE_OP_TIMEOUT

    def test_an_explicit_timeout_is_left_alone(self):
        """Only unset keywords are filled in -- a test that pins its own
        budget keeps it."""
        import redis

        client = redis.Redis(host='nonexistent-host', socket_connect_timeout=0.25)

        assert client.connection_pool.connection_kwargs['socket_connect_timeout'] == 0.25

    def test_the_default_retry_loop_is_switched_off(self):
        """redis-py 8 defaults a client to eleven attempts with an exponential
        backoff between them, which is the bulk of what an absent Redis costs
        -- 4.5s of the 4.5s once the name resolves instantly."""
        import redis

        retry = redis.Redis(host='nonexistent-host').get_retry()

        assert retry._retries == 0
        assert [retry._backoff.compute(n) for n in range(1, 4)] == [0, 0, 0]

    def test_a_ping_to_an_absent_service_fails_promptly(self):
        """The end-to-end property, at the boundary the hang happened at.

        One second is far more slack than a refused name needs and far less
        than the 4-5s a resolver round trip cost, so this fails if the guard
        stops working even though the client timeouts alone would still
        eventually return.

        Runs in the container too, which the loopback-pin version could not:
        it asserts the guard's own behaviour with the guard explicitly
        installed, rather than asserting that whatever `redis` happens to mean
        on this machine is unreachable.
        """
        import redis

        with _guard_installed():
            client = redis.Redis(host='redis', port=6379, decode_responses=True)

            started = time.monotonic()
            with pytest.raises(redis.exceptions.ConnectionError):
                client.ping()

        assert time.monotonic() - started < 1.0


class TestElasticsearchConnectIsBounded:

    def test_a_client_built_with_no_options_does_not_retry_a_dead_node(self):
        from elasticsearch import Elasticsearch

        client = Elasticsearch(['http://switchyard-no-such-host.invalid:9200'])

        # elasticsearch-py exposes the resolved values on the transport's
        # retry/timeout configuration rather than as attributes of the client.
        assert client._retry_on_timeout is False
        assert client._max_retries == 0
        assert client._request_timeout == TEST_SERVICE_OP_TIMEOUT


class TestTheGuardItself:

    def test_applying_it_twice_does_not_stack_wrappers(self):
        """pytest_configure runs once per process, but
        tests/unit/test_container_gated_reporting.py's style of nested
        invocation would otherwise wrap an already-wrapped __init__."""
        from redis.connection import AbstractConnection

        before = AbstractConnection.__init__
        _bound_service_client_timeouts()

        assert AbstractConnection.__init__ is before

    def test_only_unset_keywords_are_defaulted(self):
        class Probe:
            def __init__(self, a=None, b=None):
                self.a, self.b = a, b

        _patch_init_defaults(Probe, a='filled', b='filled')

        assert (Probe().a, Probe().b) == ('filled', 'filled')
        assert Probe(a='mine').a == 'mine'


class TestOrchestratorRootDefault:

    def test_it_redirects_INSIDE_the_container_too(self):
        """The #181 fix, and the reversal of this test's previous assertion.

        It used to assert the opposite -- that a container run was left alone,
        on the reasoning that "/app/state IS the state directory those modules
        are supposed to read". That reasoning was backwards: the documented way
        to run this suite is `pytest tests/unit` from the repository root, and
        on the deployment the repository root IS the directory bind-mounted at
        /app. So the suite wrote its fixtures into the LIVE state tree and the
        production watchdog did real work on them -- 17 files observed
        reappearing after a verified-clean deletion, each timestamped to a test
        run rather than to the orchestrator.
        """
        with patch('tests.conftest.running_in_orchestrator_container', return_value=True), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ORCHESTRATOR_ROOT', None)
            os.environ.pop('SWITCHYARD_TEST_STATE_ROOT', None)
            _redirect_orchestrator_root_to_scratch()
            root = os.environ.get('ORCHESTRATOR_ROOT')

        assert root, "a container run must still get a scratch root"
        assert root != '/app'
        assert os.path.isdir(root) and os.access(root, os.W_OK)

    def test_it_gives_a_host_run_somewhere_writable(self):
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ORCHESTRATOR_ROOT', None)
            os.environ.pop('SWITCHYARD_TEST_STATE_ROOT', None)
            _redirect_orchestrator_root_to_scratch()
            root = os.environ['ORCHESTRATOR_ROOT']

        assert os.path.isdir(root)
        assert os.access(root, os.W_OK)

    def test_the_escape_hatch_is_honoured(self):
        """SWITCHYARD_TEST_STATE_ROOT, for a run that genuinely wants a
        specific tree. No test needs it today; it exists so that needing it
        later is a one-liner rather than a reason to revert the redirect."""
        with patch('tests.conftest.running_in_orchestrator_container', return_value=True), \
                patch.dict(os.environ, {'SWITCHYARD_TEST_STATE_ROOT': '/tmp/chosen-tree'}):
            os.environ.pop('ORCHESTRATOR_ROOT', None)
            _redirect_orchestrator_root_to_scratch()

            assert os.environ['ORCHESTRATOR_ROOT'] == '/tmp/chosen-tree'

    def test_an_explicit_value_wins(self):
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False), \
                patch.dict(os.environ, {'ORCHESTRATOR_ROOT': '/somewhere/chosen'}):
            _redirect_orchestrator_root_to_scratch()

            assert os.environ['ORCHESTRATOR_ROOT'] == '/somewhere/chosen'
