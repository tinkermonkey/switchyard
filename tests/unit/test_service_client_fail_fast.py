"""
A test run cannot block on a service that is not there (#174).

On a bare python:3.11-slim runner with only `pip install -r requirements.txt`,
`pytest tests/unit` did not finish: it reached services/cancellation.py's
`redis.Redis(host='redis', port=6379, decode_responses=True).ping()` and sat
there, ~6s of CPU over 3+ minutes at ~0.2%. pytest.ini's `timeout = 300` does
not bound it, because pytest-timeout cannot interrupt a blocking socket call in
the main thread.

The obvious reading is a missing socket_connect_timeout, and ~20 call sites
across services/, monitoring/, claude/ and task_queue/ do omit one. Timing it in
that container says otherwise:

    redis.Redis(host='redis', ...).ping()                       -> 5.04s
    redis.Redis(host='redis', ..., socket_connect_timeout=2)    -> 4.16s
    ...with the name pinned to loopback, still                  -> 4.51s
    ...with retry=Retry(NoBackoff(), 0)                         -> 0.02s

Two separate costs, neither of them the connect timeout:

  - `socket.getaddrinfo`, which no client-level timeout bounds. A bare name plus
    the runner's `search` suffixes is several queries against whatever
    nameservers it was handed, and how long they take to give up is a property
    of that machine -- which is why the same suite took seconds one hour and had
    not finished in 150 the next. tests/conftest.py resolves the three compose
    service names to 127.0.0.1 off-container, which takes the resolver out of
    the path entirely.
  - redis-py 8's default client retry policy, Retry(ExponentialWithJitter
    Backoff(base=1, cap=10), retries=10). Even a connect refused instantly is
    retried eleven times with a growing sleep between them. tests/conftest.py
    defaults it to no retries and no backoff for the test run.

Both are needed; the fourth line above is what either one alone leaves behind.
"""

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
    _default_orchestrator_root_outside_the_container,
    _patch_init_defaults,
    _pin_compose_service_hostnames_to_loopback,
    running_in_orchestrator_container,
)


class TestServiceHostnamesResolveWithoutTheResolver:

    def test_it_names_the_services_this_codebase_connects_to_by_bare_hostname(self):
        for host in ('redis', 'elasticsearch'):
            assert host in COMPOSE_SERVICE_HOSTS

    def test_off_container_a_service_name_resolves_to_loopback(self):
        original = socket.getaddrinfo
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False):
            try:
                _pin_compose_service_hostnames_to_loopback()
                addresses = {info[4][0] for info in socket.getaddrinfo('redis', 6379)}
            finally:
                socket.getaddrinfo = original

        assert addresses == {'127.0.0.1'}

    def test_it_leaves_every_other_hostname_to_the_real_resolver(self):
        original = socket.getaddrinfo
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False):
            try:
                _pin_compose_service_hostnames_to_loopback()
                pinned = socket.getaddrinfo
                with patch.object(socket, 'getaddrinfo', pinned):
                    with pytest.raises(socket.gaierror):
                        socket.getaddrinfo('switchyard-no-such-host.invalid', 80)
            finally:
                socket.getaddrinfo = original

    def test_inside_the_container_the_real_hosts_are_left_alone(self):
        """`redis` and `elasticsearch` ARE real there, and the suite uses
        them -- tests/conftest.py's own test-data purge, for one."""
        original = socket.getaddrinfo
        with patch('tests.conftest.running_in_orchestrator_container', return_value=True):
            _pin_compose_service_hostnames_to_loopback()

        assert socket.getaddrinfo is original


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

    @pytest.mark.skipif(
        running_in_orchestrator_container(),
        reason="`redis` is a real, reachable host inside the orchestrator container",
    )
    def test_a_ping_to_an_absent_service_fails_promptly(self):
        """The end-to-end property, at the boundary the hang happened at.

        One second is far more slack than a refused loopback connect needs and
        far less than the 4-5s a resolver round trip cost, so this fails if the
        pin stops working even though the client timeouts alone would still
        eventually return.
        """
        import redis

        client = redis.Redis(host='redis', port=6379, decode_responses=True)

        started = time.monotonic()
        with pytest.raises(Exception):
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

    def test_it_leaves_the_container_alone(self):
        """Inside the orchestrator container /app/state IS the state directory
        those modules are supposed to read; redirecting it would change what
        every container-run test sees."""
        with patch('tests.conftest.running_in_orchestrator_container', return_value=True), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ORCHESTRATOR_ROOT', None)
            _default_orchestrator_root_outside_the_container()

            assert 'ORCHESTRATOR_ROOT' not in os.environ

    def test_it_gives_a_host_run_somewhere_writable(self):
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ORCHESTRATOR_ROOT', None)
            _default_orchestrator_root_outside_the_container()
            root = os.environ['ORCHESTRATOR_ROOT']

        assert os.path.isdir(root)
        assert os.access(root, os.W_OK)

    def test_an_explicit_value_wins(self):
        with patch('tests.conftest.running_in_orchestrator_container', return_value=False), \
                patch.dict(os.environ, {'ORCHESTRATOR_ROOT': '/somewhere/chosen'}):
            _default_orchestrator_root_outside_the_container()

            assert os.environ['ORCHESTRATOR_ROOT'] == '/somewhere/chosen'
