"""
#238: the issue->run mapping hash (`orchestrator:pipeline_run:issue_mapping`)
has no TTL of its own while the run records it points at do, and
PipelineRunManager.cleanup_expired_mappings() -- written for exactly that --
was never called by anything. The hash therefore only ever grew.

The measured composition on the reference deployment (2026-09-14) is what
shapes these tests: 9 of 13 entries had no Redis record AND could not be
resolved in Elasticsearch at all. Seven of those were fixture residue for a
`test-project` that has no config, so no ES document for them has ever existed
or ever could. "Elasticsearch returned zero hits" is therefore a routine,
expected answer -- not a death certificate -- which is why the sweep may not
delete on it and why most of what is pinned below is about NOT deleting.

The traps here all come from a previous attempt that failed review three
times; each has a test named after it:

  * deleting on absence of testimony (an ES write is swallowed by
    _persist_to_elasticsearch(), or ILM reaps the index, and the entry is
    erased rather than reported),
  * resetting the grace clock for entries that merely ERRORED, which makes one
    transient blip per window enough that an entry is never collected,
  * a corrupt-but-parsable stamp, or a forward clock jump, licensing an
    immediate delete,
  * HDEL instead of compare-and-delete, which drops a concurrently-created
    run's mapping,
  * a summary guarded by "if anything changed", which stays silent in exactly
    the steady state that matters.
"""

import json
import os
import re
from datetime import timedelta
from unittest.mock import patch

import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.pipeline_run import (
    MAPPING_MAX_PLAUSIBLE_SWEEP_GAP_SECONDS,
    MAPPING_UNRESOLVED_GRACE_SECONDS,
    MAPPING_UNRESOLVED_MIN_OBSERVATIONS,
    PipelineRunManager,
    _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT,
)


# ---------------------------------------------------------------------------
# A Lua subset interpreter, so the fake below EXECUTES the script it is handed.
#
# This is not gold-plating. A previous round of review on this code was passed
# by a FakeRedis whose eval() reimplemented compare-and-delete in Python: with
# that fake, gutting the real Lua to an unconditional HDEL kept the entire
# suite green, because no test ever ran the Lua. Parsing and evaluating the
# handful of constructs the script actually uses means the script's own text is
# what decides how these tests behave.
# ---------------------------------------------------------------------------

_LUA_TOKEN = re.compile(r"==|[\[\](),]|'[^']*'|-?\d+|[A-Za-z_][A-Za-z_0-9.]*")


def _lua_tokens(source):
    pos, out = 0, []
    while pos < len(source):
        if source[pos].isspace():
            pos += 1
            continue
        match = _LUA_TOKEN.match(source, pos)
        assert match, f"unsupported Lua syntax near {source[pos:pos + 30]!r}"
        out.append(match.group(0))
        pos = match.end()
    return out


class _LuaParser:
    """Parses `if <expr> then ... else ... end` / `return <expr>` and nothing else.

    Anything outside that grammar raises rather than being quietly ignored: a
    script rewritten in a style this cannot read must be re-examined by hand,
    not waved through.
    """

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        token = self.peek()
        assert token is not None, "unexpected end of Lua script"
        self.pos += 1
        return token

    def expect(self, token):
        got = self.next()
        assert got == token, f"expected {token!r} in Lua script, got {got!r}"

    def parse_block(self):
        statements = []
        while True:
            token = self.peek()
            if token is None or token in ('end', 'else'):
                return statements
            if token == 'return':
                self.next()
                statements.append(('return', self.parse_expr()))
            elif token == 'if':
                self.next()
                condition = self.parse_expr()
                self.expect('then')
                then_block = self.parse_block()
                else_block = []
                if self.peek() == 'else':
                    self.next()
                    else_block = self.parse_block()
                self.expect('end')
                statements.append(('if', condition, then_block, else_block))
            else:
                raise AssertionError(f"unsupported Lua statement {token!r}")

    def parse_expr(self):
        left = self.parse_primary()
        if self.peek() == '==':
            self.next()
            return ('eq', left, self.parse_primary())
        return left

    def parse_primary(self):
        token = self.next()
        if token == 'redis.call':
            self.expect('(')
            args = []
            while self.peek() != ')':
                args.append(self.parse_expr())
                if self.peek() == ',':
                    self.next()
            self.expect(')')
            return ('call', args)
        if token in ('KEYS', 'ARGV'):
            self.expect('[')
            index = int(self.next())
            self.expect(']')
            return ('index', token, index)
        if token.startswith("'"):
            return ('const', token[1:-1])
        if re.fullmatch(r'-?\d+', token):
            return ('const', int(token))
        raise AssertionError(f"unsupported Lua expression {token!r}")


class LuaScript:
    """A compiled compare-and-delete-shaped script, evaluated against a store."""

    def __init__(self, source):
        self.source = source
        self.block = _LuaParser(_lua_tokens(source)).parse_block()

    def run(self, redis_call, keys, argv):
        returned, value = self._exec(self.block, redis_call, keys, argv)
        return value if returned else None

    def _exec(self, block, redis_call, keys, argv):
        for statement in block:
            if statement[0] == 'return':
                return True, self._eval(statement[1], redis_call, keys, argv)
            _, condition, then_block, else_block = statement
            branch = then_block if self._eval(condition, redis_call, keys, argv) else else_block
            returned, value = self._exec(branch, redis_call, keys, argv)
            if returned:
                return True, value
        return False, None

    def _eval(self, node, redis_call, keys, argv):
        kind = node[0]
        if kind == 'const':
            return node[1]
        if kind == 'index':
            source = keys if node[1] == 'KEYS' else argv
            return source[node[2] - 1]
        if kind == 'eq':
            left = self._eval(node[1], redis_call, keys, argv)
            right = self._eval(node[2], redis_call, keys, argv)
            # Lua: a nil from redis.call compares equal to nothing a caller can
            # pass in ARGV, which is what makes the guard work on a missing field.
            if left is None or right is None:
                return left is None and right is None
            return left == right
        if kind == 'call':
            args = [self._eval(arg, redis_call, keys, argv) for arg in node[1]]
            return redis_call(*args)
        raise AssertionError(f"unsupported Lua node {node!r}")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory Redis with the command surface this sweep uses.

    Strings and hashes live in separate namespaces so a hash operation on a
    string key would be a KeyError here rather than silently plausible.
    """

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.hdel_calls = []
        self.eval_calls = []
        self.fail_get_keys = set()
        self.fail_hgetall_keys = set()
        self.before_eval = None

    # --- string ---------------------------------------------------------
    def get(self, key):
        if key in self.fail_get_keys:
            raise ConnectionError(f"simulated Redis failure reading {key}")
        return self.strings.get(key)

    def setex(self, key, ttl, value):
        self.strings[key] = value
        return True

    # --- hash -----------------------------------------------------------
    def hgetall(self, key):
        if key in self.fail_hgetall_keys:
            raise ConnectionError(f"simulated Redis failure reading {key}")
        return dict(self.hashes.get(key, {}))

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hdel(self, key, field):
        # Direct calls only. A delete performed BY the Lua script goes through
        # _hdel_raw, so "was this hash ever touched with an unconditional HDEL"
        # stays an answerable question.
        self.hdel_calls.append((key, field))
        return self._hdel_raw(key, field)

    def _hdel_raw(self, key, field):
        return 1 if self.hashes.get(key, {}).pop(field, None) is not None else 0

    # --- scripting ------------------------------------------------------
    def eval(self, script, numkeys, *args):
        self.eval_calls.append((script, numkeys, args))
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        if self.before_eval is not None:
            self.before_eval(keys, argv)

        def redis_call(command, *call_args):
            command = command.upper()
            if command == 'HGET':
                return self.hget(call_args[0], call_args[1])
            if command == 'HDEL':
                return self._hdel_raw(call_args[0], call_args[1])
            raise AssertionError(f"unexpected redis.call({command!r}) from Lua")

        return LuaScript(script).run(redis_call, keys, argv)


class FakeElasticsearch:
    """search() that answers the sweep's id lookup, and can refuse to answer."""

    def __init__(self):
        self.docs = {}
        self.fail = False
        self.searches = 0

    def index_run(self, run_id, status, ended_at=None, index='pipeline-runs-2026-09-01'):
        self.docs[run_id] = {
            'id': run_id,
            'status': status,
            'ended_at': ended_at,
            'started_at': '2026-09-01T00:00:00Z',
        }

    def search(self, index, body):
        self.searches += 1
        if self.fail:
            raise ConnectionError("simulated Elasticsearch failure")
        should = body['query']['bool']['should']
        wanted = set()
        for clause in should:
            if 'ids' in clause:
                wanted.update(clause['ids']['values'])
            elif 'term' in clause:
                wanted.add(clause['term']['id'])
        hits = [
            {'_id': doc_id, '_source': source}
            for doc_id, source in self.docs.items()
            if doc_id in wanted or source.get('id') in wanted
        ]
        size = body.get('size', 10)
        return {'hits': {'total': {'value': len(hits)}, 'hits': hits[:size]}}


MAPPING = 'orchestrator:pipeline_run:issue_mapping'
STAMPS = 'orchestrator:pipeline_run:issue_mapping:unresolved_since'


def make_manager(with_es=True):
    redis_client = FakeRedis()
    es_client = FakeElasticsearch() if with_es else None
    with patch('services.pipeline_run.Elasticsearch', side_effect=AssertionError(
            "the sweep must not reach for a real Elasticsearch client")):
        manager = PipelineRunManager(
            redis_client=redis_client,
            elasticsearch_client=es_client,
            manage_schema=False,
        )
    # PipelineRunManager falls back to a real client when passed None; for the
    # no-Elasticsearch case we want the attribute genuinely empty.
    manager.es = es_client
    return manager, redis_client, es_client


def run_blob(run_id, status='active', ended_at=None):
    return json.dumps({
        'id': run_id,
        'issue_number': 42,
        'issue_title': 'title',
        'issue_url': 'https://example.invalid/42',
        'project': 'proj',
        'board': 'dev',
        'started_at': '2026-09-01T00:00:00Z',
        'ended_at': ended_at,
        'status': status,
    })


class Clock:
    """The seam cleanup_expired_mappings() reads wall-clock through."""

    def __init__(self, start=1_780_000_000.0):
        self.now = start

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def sweep(manager, clock, **kwargs):
    with patch('services.pipeline_run._now_epoch_seconds', clock):
        return manager.cleanup_expired_mappings(**kwargs)


class TestTheFakeExecutesTheRealScript:
    """The harness itself, because everything below leans on it."""

    def test_the_real_script_is_compare_and_delete(self):
        redis_client = FakeRedis()
        redis_client.hset('h', 'f', 'run-a')

        assert redis_client.eval(
            _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT, 1, 'h', 'f', 'run-b'
        ) == 0
        assert redis_client.hget('h', 'f') == 'run-a'

        assert redis_client.eval(
            _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT, 1, 'h', 'f', 'run-a'
        ) == 1
        assert redis_client.hget('h', 'f') is None

    def test_a_gutted_script_behaves_differently_here(self):
        """If this ever stops holding, the fake has become a rubber stamp and
        every compare-and-delete assertion in this file is worthless."""
        redis_client = FakeRedis()
        redis_client.hset('h', 'f', 'run-a')

        gutted = "return redis.call('HDEL', KEYS[1], ARGV[1])"
        assert redis_client.eval(gutted, 1, 'h', 'f', 'run-b') == 1
        assert redis_client.hget('h', 'f') is None

    def test_a_missing_field_is_not_deleted(self):
        redis_client = FakeRedis()
        assert redis_client.eval(
            _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT, 1, 'h', 'absent', 'run-a'
        ) == 0


class TestEvidenceCollectsImmediately:
    """Positive testimony that a run has ended needs no grace period."""

    def test_completed_run_in_redis_is_collected(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        redis_client.strings[manager._get_redis_key('run-a')] = run_blob(
            'run-a', status='completed', ended_at='2026-09-02T00:00:00Z')

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {}
        assert summary['deleted_ended'] == 1
        assert summary['deleted_unaccountable'] == 0

    def test_failed_run_in_elasticsearch_is_collected(self):
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        es.index_run('run-a', status='failed', ended_at='2026-09-02T00:00:00Z')

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {}
        assert summary['deleted_ended'] == 1

    def test_active_run_in_redis_is_left_alone(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        redis_client.strings[manager._get_redis_key('run-a')] = run_blob('run-a')

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-a'}
        assert summary['held_active'] == 1

    def test_feedback_listening_run_is_left_alone(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        redis_client.strings[manager._get_redis_key('run-a')] = run_blob(
            'run-a', status='feedback_listening')

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-a'}
        assert summary['held_active'] == 1

    def test_run_active_only_in_elasticsearch_is_left_alone(self):
        """The #233 population: a long run whose Redis record has expired."""
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        es.index_run('run-a', status='active')

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-a'}
        assert summary['held_active'] == 1

    def test_an_unrecognised_status_is_not_treated_as_ended(self):
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        es.index_run('run-a', status='something_new')

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-a'}
        assert summary['undetermined'] == 1

    def test_a_duplicate_document_saying_active_wins_over_one_saying_ended(self):
        """Being wrong about 'active' costs one more sweep; being wrong about
        'ended' costs a live run its mapping."""
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        es.docs['run-a'] = {'id': 'run-a', 'status': 'completed',
                            'started_at': '2026-09-01T00:00:00Z'}
        es.docs['dup'] = {'id': 'run-a', 'status': 'active',
                          'started_at': '2026-09-02T00:00:00Z'}

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-a'}
        assert summary['held_active'] == 1


class TestCompareAndDeleteNotHdel:

    def test_deletion_goes_through_the_shared_compare_and_delete_script(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        redis_client.strings[manager._get_redis_key('run-a')] = run_blob(
            'run-a', status='completed', ended_at='2026-09-02T00:00:00Z')

        sweep(manager, Clock())

        assert redis_client.eval_calls == [
            (_COMPARE_AND_DELETE_HASH_FIELD_SCRIPT, 1, (MAPPING, 'proj:dev:42', 'run-a')),
        ]
        assert not [call for call in redis_client.hdel_calls if call[0] == MAPPING], (
            "the mapping hash must never be touched with an unconditional HDEL"
        )

    def test_a_field_repointed_mid_sweep_keeps_the_new_run(self):
        """get_or_create_pipeline_run() can repoint the field between this
        sweep's HGETALL and its delete. The new run's mapping must survive."""
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        redis_client.strings[manager._get_redis_key('run-a')] = run_blob(
            'run-a', status='completed', ended_at='2026-09-02T00:00:00Z')

        def repoint(keys, argv):
            redis_client.hashes[MAPPING]['proj:dev:42'] = 'run-b'

        redis_client.before_eval = repoint

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-b'}
        assert summary['deleted_ended'] == 0
        assert summary['raced'] == 1


class TestSilenceIsNotEvidence:
    """Zero hits means nothing testified, not that the run is gone."""

    def _unaccountable(self):
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'test-project:dev:500', 'run-ghost')
        return manager, redis_client, es

    def test_an_unaccountable_entry_survives_its_first_sweep(self):
        manager, redis_client, _ = self._unaccountable()

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'test-project:dev:500': 'run-ghost'}
        assert summary['held_unaccountable'] == 1
        assert summary['deleted_unaccountable'] == 0

    def test_it_survives_right_up_to_the_grace_boundary(self):
        manager, redis_client, _ = self._unaccountable()
        clock = Clock()
        step = 1800

        elapsed = 0
        while elapsed + step < MAPPING_UNRESOLVED_GRACE_SECONDS:
            sweep(manager, clock)
            clock.advance(step)
            elapsed += step

        assert redis_client.hashes[MAPPING] == {'test-project:dev:500': 'run-ghost'}

    def test_it_is_collected_once_the_grace_has_actually_been_watched(self):
        manager, redis_client, _ = self._unaccountable()
        clock = Clock()

        collected = False
        for _ in range(200):
            summary = sweep(manager, clock)
            if summary['deleted_unaccountable']:
                collected = True
                break
            clock.advance(1800)

        assert collected, "debris must eventually be collected, or the hash still grows"
        assert redis_client.hashes[MAPPING] == {}
        assert redis_client.hashes.get(STAMPS, {}) == {}

    def test_a_single_look_never_collects_however_long_the_process_has_run(self):
        """First sighting carries no accrued time, so a stamp written now and a
        clock read hours later is not a licence."""
        manager, redis_client, _ = self._unaccountable()
        clock = Clock()

        sweep(manager, clock)
        clock.advance(MAPPING_UNRESOLVED_GRACE_SECONDS * 10)
        summary = sweep(manager, clock)

        assert redis_client.hashes[MAPPING] == {'test-project:dev:500': 'run-ghost'}
        assert summary['deleted_unaccountable'] == 0


class TestErrorsMustNotResetTheClock:
    """Constraint 3: one transient blip per window must not mean 'never'."""

    def test_an_elasticsearch_failure_leaves_the_accrued_state_untouched(self):
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        clock = Clock()

        sweep(manager, clock)
        clock.advance(1800)
        sweep(manager, clock)
        before = redis_client.hashes[STAMPS]['proj:dev:42']

        clock.advance(1800)
        es.fail = True
        summary = sweep(manager, clock)
        es.fail = False

        assert summary['errors'] == 1
        assert redis_client.hashes[STAMPS]['proj:dev:42'] == before, (
            "an errored pass learned nothing; it must not rewrite the clock"
        )

    def test_a_blip_every_few_passes_still_ends_in_collection(self):
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        clock = Clock()

        collected = False
        for index in range(400):
            es.fail = (index % 4 == 3)
            summary = sweep(manager, clock)
            es.fail = False
            if summary['deleted_unaccountable']:
                collected = True
                break
            clock.advance(1800)

        assert collected, (
            "clearing the stamp on error makes a flaky store equivalent to "
            "never collecting, with silent growth as the only symptom"
        )

    def test_one_unreachable_entry_does_not_stop_the_others(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:1', 'run-broken')
        redis_client.hset(MAPPING, 'proj:dev:2', 'run-done')
        redis_client.fail_get_keys.add(manager._get_redis_key('run-broken'))
        redis_client.strings[manager._get_redis_key('run-done')] = run_blob(
            'run-done', status='completed', ended_at='2026-09-02T00:00:00Z')

        summary = sweep(manager, Clock())

        assert summary['errors'] == 1
        assert summary['deleted_ended'] == 1
        assert redis_client.hashes[MAPPING] == {'proj:dev:1': 'run-broken'}

    def test_without_elasticsearch_nothing_is_ever_collected(self):
        """No ES client means the question cannot be put at all. That is not
        the same as an answer, however many times it is not asked."""
        manager, redis_client, _ = make_manager(with_es=False)
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        clock = Clock()

        for _ in range(100):
            summary = sweep(manager, clock)
            clock.advance(1800)

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-ghost'}
        assert summary['undetermined'] == 1
        assert summary['deleted_unaccountable'] == 0

    def test_an_unreadable_run_record_is_undetermined_not_ended(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        redis_client.strings[manager._get_redis_key('run-a')] = '{not json'

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-a'}
        assert summary['undetermined'] == 1


class TestStampIntegrity:
    """Constraint 4: a stamp that parses is not a stamp that can be trusted."""

    @pytest.mark.parametrize('raw', [
        '',
        'not json at all',
        '"a string"',
        '0:',
        json.dumps({'unresolved_seconds': 999999.0, 'observations': 99}),
        json.dumps({'pipeline_run_id': 'run-ghost', 'unresolved_seconds': float('inf'),
                    'observations': 99, 'last_seen_at': 1.0}),
        json.dumps({'pipeline_run_id': 'run-ghost', 'unresolved_seconds': float('-inf'),
                    'observations': 99, 'last_seen_at': 1.0}),
        json.dumps({'pipeline_run_id': 'run-ghost', 'unresolved_seconds': float('nan'),
                    'observations': 99, 'last_seen_at': 1.0}),
        json.dumps({'pipeline_run_id': 'run-ghost', 'unresolved_seconds': 1e18,
                    'observations': 99, 'last_seen_at': 1.0}),
        json.dumps({'pipeline_run_id': 'run-ghost', 'unresolved_seconds': -5,
                    'observations': 99, 'last_seen_at': 1.0}),
        json.dumps({'pipeline_run_id': 'run-ghost', 'unresolved_seconds': 0,
                    'observations': 0, 'last_seen_at': 1.0}),
        json.dumps({'pipeline_run_id': 'run-ghost',
                    'unresolved_seconds': MAPPING_UNRESOLVED_GRACE_SECONDS * 10,
                    'observations': 99, 'last_seen_at': 1.0}),
    ])
    def test_a_corrupt_stamp_does_not_license_a_delete(self, raw):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        redis_client.hset(STAMPS, 'proj:dev:42', raw)

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-ghost'}
        assert summary['deleted_unaccountable'] == 0
        rewritten = json.loads(redis_client.hashes[STAMPS]['proj:dev:42'])
        assert rewritten['observations'] == 1
        assert rewritten['unresolved_seconds'] == 0

    def test_a_stamp_for_a_different_run_restarts_the_clock(self):
        """The field was repointed; the old run's accrued time says nothing
        about the new one."""
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-new')
        redis_client.hset(STAMPS, 'proj:dev:42', json.dumps({
            'pipeline_run_id': 'run-old',
            'unresolved_seconds': MAPPING_UNRESOLVED_GRACE_SECONDS,
            'observations': 500,
            'last_seen_at': 1_780_000_000.0,
        }))

        summary = sweep(manager, Clock())

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-new'}
        assert summary['deleted_unaccountable'] == 0
        assert json.loads(redis_client.hashes[STAMPS]['proj:dev:42'])['observations'] == 1

    def test_a_forward_clock_jump_past_the_grace_window_collects_nothing(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        clock = Clock()

        sweep(manager, clock)
        clock.advance(MAPPING_UNRESOLVED_GRACE_SECONDS * 3)
        summary = sweep(manager, clock)
        clock.advance(1800)
        summary = sweep(manager, clock)

        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-ghost'}
        assert summary['deleted_unaccountable'] == 0
        accrued = json.loads(redis_client.hashes[STAMPS]['proj:dev:42'])['unresolved_seconds']
        assert accrued <= MAPPING_MAX_PLAUSIBLE_SWEEP_GAP_SECONDS + 1800, (
            "time nobody watched must not accrue"
        )

    def test_a_backwards_clock_accrues_nothing_and_does_not_crash(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        clock = Clock()

        sweep(manager, clock)
        clock.advance(-50000)
        summary = sweep(manager, clock)

        assert summary['deleted_unaccountable'] == 0
        assert json.loads(
            redis_client.hashes[STAMPS]['proj:dev:42'])['unresolved_seconds'] == 0

    def test_downtime_longer_than_a_plausible_gap_does_not_accrue(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        clock = Clock()

        sweep(manager, clock)
        clock.advance(MAPPING_MAX_PLAUSIBLE_SWEEP_GAP_SECONDS + 60)
        sweep(manager, clock)

        assert json.loads(
            redis_client.hashes[STAMPS]['proj:dev:42'])['unresolved_seconds'] == 0

    def test_min_observations_is_required_on_top_of_the_grace(self):
        """Even with the full grace already accrued, a single further look is
        not enough -- a pass count is the one thing a clock cannot fabricate."""
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-ghost')
        clock = Clock()
        redis_client.hset(STAMPS, 'proj:dev:42', json.dumps({
            'pipeline_run_id': 'run-ghost',
            'unresolved_seconds': MAPPING_UNRESOLVED_GRACE_SECONDS,
            'observations': 1,
            'last_seen_at': clock.now,
        }))

        summary = sweep(manager, clock, min_observations=MAPPING_UNRESOLVED_MIN_OBSERVATIONS)

        assert MAPPING_UNRESOLVED_MIN_OBSERVATIONS > 2, (
            "a minimum of 1 or 2 observations would not survive a clock jump"
        )
        assert summary['deleted_unaccountable'] == 0
        assert redis_client.hashes[MAPPING] == {'proj:dev:42': 'run-ghost'}


class TestTheSweepDoesNotGrowItsOwnDebris:

    def test_stamps_for_vanished_entries_are_pruned(self):
        manager, redis_client, _ = make_manager()
        redis_client.hset(STAMPS, 'proj:dev:gone', json.dumps({
            'pipeline_run_id': 'run-x', 'unresolved_seconds': 10,
            'observations': 2, 'last_seen_at': 1_780_000_000.0,
        }))

        summary = sweep(manager, Clock())

        assert redis_client.hashes[STAMPS] == {}
        assert summary['stamps_pruned'] == 1

    def test_a_stamp_is_dropped_once_the_entry_is_accounted_for(self):
        manager, redis_client, es = make_manager()
        redis_client.hset(MAPPING, 'proj:dev:42', 'run-a')
        clock = Clock()

        sweep(manager, clock)
        assert 'proj:dev:42' in redis_client.hashes[STAMPS]

        es.index_run('run-a', status='active')
        clock.advance(1800)
        sweep(manager, clock)

        assert redis_client.hashes[STAMPS] == {}

    def test_an_unreadable_mapping_hash_aborts_without_deleting(self):
        manager, redis_client, _ = make_manager()
        redis_client.fail_hgetall_keys.add(MAPPING)

        summary = sweep(manager, Clock())

        assert summary['errors'] == 1
        assert summary['examined'] == 0
        assert redis_client.eval_calls == []

    def test_an_unreadable_mapping_hash_does_not_discard_accrued_grace(self):
        """Constraint 3 again, at the one place it is easy to miss.

        If the sweep cannot read the mapping it knows NOTHING about which
        entries still exist -- so it must abort, not carry on with an empty
        mapping. Carrying on looks harmless (it deletes nothing) but every
        stamp then reads as state for a vanished entry and is pruned, so one
        unreadable HGETALL per window resets every entry's clock and nothing is
        ever collected. Pinning "deleted nothing" alone does not catch that.
        """
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'test-project:dev:500', 'run-ghost')
        stamp = json.dumps({
            'pipeline_run_id': 'run-ghost',
            'unresolved_seconds': MAPPING_UNRESOLVED_GRACE_SECONDS - 60,
            'observations': 9,
            'last_seen_at': 1_780_000_000.0,
        })
        redis_client.hset(STAMPS, 'test-project:dev:500', stamp)
        redis_client.fail_hgetall_keys.add(MAPPING)

        summary = sweep(manager, Clock())

        assert summary['errors'] == 1
        assert summary['stamps_pruned'] == 0
        assert redis_client.hashes[STAMPS] == {'test-project:dev:500': stamp}, (
            "an unreadable mapping hash must not be read as 'no entries exist' "
            "and take every accrued grace window with it"
        )
        assert redis_client.hdel_calls == []


class TestSteadyStateIsLogged:
    """Constraint 6: the silent state is the one that matters."""

    def test_a_pass_that_changes_nothing_still_reports_what_is_held(self, caplog):
        manager, redis_client, _ = make_manager()
        for issue in (500, 501, 503):
            redis_client.hset(MAPPING, f'test-project:dev:{issue}', f'run-{issue}')

        with caplog.at_level('INFO', logger='services.pipeline_run'):
            summary = sweep(manager, Clock())

        assert summary['deleted_ended'] == 0
        assert summary['deleted_unaccountable'] == 0
        assert summary['held_unaccountable'] == 3

        messages = '\n'.join(caplog.messages)
        assert '3 entries examined' in messages
        assert '3 unaccountable' in messages
        assert 'test-project:dev:500' in messages, (
            "the held entries must be nameable from the log, not just counted"
        )

    def test_the_held_entries_are_reported_at_warning_level(self, caplog):
        manager, redis_client, _ = make_manager()
        redis_client.hset(MAPPING, 'test-project:dev:500', 'run-ghost')

        with caplog.at_level('INFO', logger='services.pipeline_run'):
            sweep(manager, Clock())

        warnings = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
        assert any('neither Redis nor Elasticsearch can account for' in message
                   for message in warnings)

    def test_a_clean_hash_still_logs(self, caplog):
        manager, _, _ = make_manager()

        with caplog.at_level('INFO', logger='services.pipeline_run'):
            sweep(manager, Clock())

        assert any('0 entries examined' in message for message in caplog.messages)


class TestTheMeasuredDeployment:
    """The 13 entries measured on 2026-09-14, end to end."""

    def test_the_reference_hash_is_collected_without_touching_the_live_run(self):
        manager, redis_client, es = make_manager()
        debris = {
            'test-project:dev:500': '2081efb4',
            'test-project:dev:501': '01a8d407',
            'test-project:dev:503': '23aa06a5',
            'test-project:dev:600': '5d4fc889',
            'test-project:dev:700': '8e028102',
            'test-project:dev:701': '2834c79d',
            'test-project:dev:2100': '925c5b5b',
            'context-studio:Planning & Design:1140': 'ba115fd5',
            'context-studio:1140': 'ba115fd5',
        }
        for key, run_id in debris.items():
            redis_client.hset(MAPPING, key, run_id)
        # ...alongside one genuinely live run, only Elasticsearch knows about.
        redis_client.hset(MAPPING, 'codetoreum:Planning & Design:1017', 'run-live')
        es.index_run('run-live', status='active')

        clock = Clock()
        for _ in range(200):
            summary = sweep(manager, clock)
            if summary['held_unaccountable'] == 0:
                break
            clock.advance(1800)

        assert redis_client.hashes[MAPPING] == {
            'codetoreum:Planning & Design:1017': 'run-live',
        }
        assert redis_client.hashes.get(STAMPS, {}) == {}


class TestTheSweepIsActuallyWired:
    """The original defect was not a wrong sweep; it was a correct sweep that
    nothing ever called. A `grep` for cleanup_expired_mappings returned only
    its own definition. These pin the call site itself."""

    @pytest.mark.asyncio
    async def test_the_job_is_registered(self):
        from services.scheduled_tasks import ScheduledTasksService

        service = ScheduledTasksService()
        service.start()
        try:
            job = service.scheduler.get_job('pipeline_run_mapping_cleanup')
            assert job is not None, "the sweep is dead code again"
            # It has to run often enough to actually WATCH an entry: the grace
            # accrues only over gaps the sweep itself observed, and a gap
            # longer than MAPPING_MAX_PLAUSIBLE_SWEEP_GAP_SECONDS accrues
            # nothing at all, so a cadence near that bound never collects.
            assert job.trigger.interval <= timedelta(
                seconds=MAPPING_MAX_PLAUSIBLE_SWEEP_GAP_SECONDS / 2)
        finally:
            service.stop()

    @pytest.mark.asyncio
    async def test_running_the_job_sweeps_the_mappings(self):
        from unittest.mock import MagicMock
        import services.scheduled_tasks as scheduled_tasks

        service = scheduled_tasks.ScheduledTasksService()
        manager = MagicMock()
        manager.cleanup_expired_mappings.return_value = {'examined': 0}

        # The second patch is a guard, not scaffolding: if the first one ever
        # stops intercepting, an unpatched get_pipeline_run_manager() builds a
        # REAL client pointed at the deployment's Redis and this test starts
        # deleting live mapping entries.
        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=manager), \
             patch('services.pipeline_run.PipelineRunManager',
                   side_effect=AssertionError("must not build a real manager")):
            await service._cleanup_pipeline_run_mappings()

        manager.cleanup_expired_mappings.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_the_job_is_the_one_that_sweeps(self):
        """Registered under that id AND pointing at the sweep -- either alone
        would let the two drift apart."""
        from unittest.mock import MagicMock
        from services.scheduled_tasks import ScheduledTasksService

        service = ScheduledTasksService()
        service.start()
        try:
            job = service.scheduler.get_job('pipeline_run_mapping_cleanup')
            manager = MagicMock()
            manager.cleanup_expired_mappings.return_value = {'examined': 0}
            with patch('services.pipeline_run.get_pipeline_run_manager', return_value=manager), \
                 patch('services.pipeline_run.PipelineRunManager',
                       side_effect=AssertionError("must not build a real manager")):
                await job.func()
        finally:
            service.stop()

        manager.cleanup_expired_mappings.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_failing_sweep_never_escapes_the_job(self, caplog):
        from services.scheduled_tasks import ScheduledTasksService

        service = ScheduledTasksService()
        with patch('services.pipeline_run.get_pipeline_run_manager',
                   side_effect=RuntimeError("redis is gone")):
            with caplog.at_level('ERROR', logger='services.scheduled_tasks'):
                await service._cleanup_pipeline_run_mappings()

        assert any('mapping sweep failed' in message for message in caplog.messages)
