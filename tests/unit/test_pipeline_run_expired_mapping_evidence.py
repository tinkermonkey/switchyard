"""get_active_pipeline_run() must not delete an issue mapping on Redis silence (#239).

The mapping hash `orchestrator:pipeline_run:issue_mapping` is the index from
(project[, board], issue) to a run id. Once a run's own Redis record is gone it
is also the ONLY thing that still knows which run belongs to that issue:
get_active_run_workspaces() iterates exactly this hash, and its answer is what
stops the startup sweep deleting an in-flight run's epic worktree (#233).

The old code deleted the entry whenever the run's record was missing from
Redis -- before Elasticsearch had been asked anything, and regardless of what
ES went on to say. A missing record is not evidence that a run ended; it is
evidence of nothing at all (a seven-day TTL elapsed under a live run, an
eviction, a flushed Redis). These tests pin the replacement rule: the entry is
removed only on POSITIVE evidence -- ES holds that run's document and that
document says the run ended.
"""

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from services.pipeline_run import (
    _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT,
    PipelineRunManager,
)


class LuaSubsetError(AssertionError):
    """The script used Lua this runner does not implement.

    Raised, never swallowed: a script the runner cannot execute has to surface
    as a loud test error, because the alternative -- quietly doing nothing --
    is indistinguishable from a passing test.
    """


_LUA_TOKENS = re.compile(
    r"""\s+
      | (?P<string>'[^']*')
      | (?P<number>\d+)
      | (?P<name>[A-Za-z_]\w*)
      | (?P<op>==|~=|[.,()\[\]])""",
    re.VERBOSE,
)


class _LuaSubset:
    """Executes the small Lua subset this module's Redis scripts are written in.

    Script-AGNOSTIC by construction: it implements `if/then/else/end`, `return`,
    `KEYS`/`ARGV` indexing, literals, `==`/`~=` and `redis.call(...)`, and knows
    nothing about compare-and-delete or any other particular script. That is the
    whole point -- a fake that reimplemented the script's logic in Python would
    keep passing after the real Lua was gutted, which is exactly the hole these
    tests exist to close. Anything outside the subset raises LuaSubsetError.
    """

    _NO_RETURN = object()

    def __init__(self, script, keys, argv, call):
        self.tokens = self._tokenize(script)
        self.pos = 0
        self.keys = list(keys)
        self.argv = list(argv)
        self.call = call

    @staticmethod
    def _tokenize(script):
        tokens, at = [], 0
        while at < len(script):
            match = _LUA_TOKENS.match(script, at)
            if not match:
                raise LuaSubsetError(
                    f"unsupported Lua syntax at offset {at}: {script[at:at + 40]!r}"
                )
            at = match.end()
            for kind in ('string', 'number', 'name', 'op'):
                text = match.group(kind)
                if text is not None:
                    tokens.append((kind, text))
                    break
        return tokens

    # -- token helpers -----------------------------------------------------
    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def _take(self):
        token = self._peek()
        self.pos += 1
        return token

    def _expect(self, text):
        _, found = self._take()
        if found != text:
            raise LuaSubsetError(f"expected {text!r}, found {found!r}")

    # -- parser ------------------------------------------------------------
    def _parse_block(self, terminators):
        statements = []
        while True:
            _, word = self._peek()
            if word is None or word in terminators:
                return statements
            statements.append(self._parse_statement())

    def _parse_statement(self):
        _, word = self._peek()
        if word == 'return':
            self._take()
            return ('return', self._parse_expression())
        if word == 'if':
            self._take()
            condition = self._parse_expression()
            self._expect('then')
            consequent = self._parse_block({'else', 'elseif', 'end'})
            alternative = []
            if self._peek()[1] == 'else':
                self._take()
                alternative = self._parse_block({'end'})
            self._expect('end')
            return ('if', condition, consequent, alternative)
        raise LuaSubsetError(f"unsupported statement starting at {word!r}")

    def _parse_expression(self):
        left = self._parse_primary()
        _, operator = self._peek()
        if operator in ('==', '~='):
            self._take()
            return (operator, left, self._parse_primary())
        return left

    def _parse_primary(self):
        kind, text = self._take()
        if kind == 'string':
            return ('const', text[1:-1])
        if kind == 'number':
            return ('const', int(text))
        if kind == 'name':
            if text in ('KEYS', 'ARGV'):
                self._expect('[')
                index_kind, index = self._take()
                self._expect(']')
                if index_kind != 'number':
                    raise LuaSubsetError(f"non-literal {text} index: {index!r}")
                return ('index', text, int(index))
            if text == 'redis':
                self._expect('.')
                _, function = self._take()
                if function not in ('call', 'pcall'):
                    raise LuaSubsetError(f"unsupported redis function: {function!r}")
                self._expect('(')
                arguments = []
                if self._peek()[1] != ')':
                    arguments.append(self._parse_expression())
                    while self._peek()[1] == ',':
                        self._take()
                        arguments.append(self._parse_expression())
                self._expect(')')
                return ('call', arguments)
            if text in ('nil', 'true', 'false'):
                return ('const', {'nil': None, 'true': True, 'false': False}[text])
        raise LuaSubsetError(f"unsupported expression starting at {text!r}")

    # -- evaluator ---------------------------------------------------------
    def _evaluate(self, node):
        kind = node[0]
        if kind == 'const':
            return node[1]
        if kind == 'index':
            values = self.keys if node[1] == 'KEYS' else self.argv
            position = node[2]
            return values[position - 1] if 1 <= position <= len(values) else None
        if kind == 'call':
            return self.call(*[self._evaluate(argument) for argument in node[1]])
        left, right = self._evaluate(node[1]), self._evaluate(node[2])
        equal = left is right if left is None or right is None else left == right
        return equal if kind == '==' else not equal

    def _execute(self, statements):
        for statement in statements:
            if statement[0] == 'return':
                return self._evaluate(statement[1])
            condition = self._evaluate(statement[1])
            branch = statement[2] if condition not in (None, False) else statement[3]
            result = self._execute(branch)
            if result is not self._NO_RETURN:
                return result
        return self._NO_RETURN

    def run(self):
        body = self._parse_block(set())
        if self.pos != len(self.tokens):
            raise LuaSubsetError(f"trailing tokens from {self.tokens[self.pos]!r}")
        result = self._execute(body)
        return None if result is self._NO_RETURN else result


class FakeRedis:
    """Minimal in-memory Redis.

    Deliberately dumb: it stores and returns values and does not model TTLs
    (tests simulate expiry by deleting the key, which is what a TTL produces)
    or reimplement anything the code under test decides.
    """

    def __init__(self):
        self.data = {}
        self.evals = []  # (script, numkeys, *args) for every eval() call

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value
        return True

    def hget(self, key, field):
        bucket = self.data.get(key)
        return bucket.get(field) if isinstance(bucket, dict) else None

    def hset(self, key, field, value):
        self.data.setdefault(key, {})[field] = value
        return 1

    def hgetall(self, key):
        bucket = self.data.get(key)
        return dict(bucket) if isinstance(bucket, dict) else {}

    def hdel(self, key, field):
        bucket = self.data.get(key)
        if isinstance(bucket, dict):
            bucket.pop(field, None)
        return 1

    def eval(self, script, numkeys, *args):
        """Stand in for the Redis server by EXECUTING the caller's script text.

        Nothing here knows what the script is for. The Lua the module ships is
        parsed and run by _LuaSubset against this store, so the script body is
        under test too: replace the compare-and-delete with a bare HDEL and
        TestReapUsesCompareAndDelete fails. A fake that instead reimplemented
        compare-and-delete in Python would pass either way -- that is the trap
        this avoids, and the recorded `evals` below let a test assert on the
        call the caller actually made.
        """
        self.evals.append((script, numkeys, *args))
        return _LuaSubset(
            script, args[:numkeys], args[numkeys:], self._redis_call
        ).run()

    def _redis_call(self, command, *args):
        """Dispatch a redis.call() from a script to this store."""
        handler = {
            'HGET': self.hget,
            'HDEL': self.hdel,
            'HSET': self.hset,
            'GET': self.get,
            'DEL': self.delete,
            'EXISTS': self.exists,
        }.get(str(command).upper())
        if handler is None:
            raise AssertionError(f"script called an unmodelled Redis command: {command!r}")
        return handler(*args)

    def delete(self, key):
        self.data.pop(key, None)
        return 1

    def exists(self, key):
        return key in self.data

    def lock(self, name, timeout=None, blocking_timeout=None):
        return _NoopLock()


class _NoopLock:
    def __enter__(self):
        return True

    def __exit__(self, *exc_info):
        return False


class FakeElasticsearch:
    """An ES client whose index CONTENTS are controlled by the test.

    index() is a recorded no-op: writes do not become searchable. That models
    the population this fix is about -- a run whose ES document is gone (rolled
    out of its retention window, restored-from-elsewhere cluster) while its
    Redis mapping entry survives -- and keeps a test's ES state explicit rather
    than accumulating whatever production code happened to write.

    Use seed() to put a document in the index.
    """

    def __init__(self, fail_search=False):
        self.docs_by_id = {}
        self.searches = []
        self.indexed = []
        self.fail_search = fail_search
        self.on_search = None
        self.ilm = MagicMock()
        self.indices = MagicMock()
        self.indices.exists.return_value = False

    def seed(self, doc):
        self.docs_by_id[doc['id']] = doc

    def index(self, index, id=None, document=None, body=None, **kwargs):
        self.indexed.append(id)
        return {'result': 'created'}

    def search(self, index, body):
        self.searches.append(body)
        if self.on_search:
            self.on_search(body)
        if self.fail_search:
            raise RuntimeError("elasticsearch unavailable")

        query = body['query']
        clauses = query['bool']['must'] if 'bool' in query else [query]

        def matches(doc):
            for clause in clauses:
                if 'term' in clause:
                    (field, value), = clause['term'].items()
                    actual = doc['id'] if field == '_id' else doc.get(field)
                    if actual != value:
                        return False
                if 'terms' in clause:
                    (field, values), = clause['terms'].items()
                    if doc.get(field) not in values:
                        return False
            return True

        hits = [d for d in self.docs_by_id.values() if matches(d)]
        hits.sort(key=lambda d: d.get('started_at', ''), reverse=True)
        size = body.get('size', 10)
        return {
            'hits': {
                'total': {'value': len(hits)},
                'hits': [{'_source': h, '_id': h['id']} for h in hits[:size]],
            }
        }


def make_manager(es=None):
    fake_redis = FakeRedis()
    fake_es = FakeElasticsearch() if es is None else es
    with patch('services.pipeline_run.Elasticsearch', return_value=fake_es), \
         patch('services.pipeline_run.redis.Redis', return_value=fake_redis):
        manager = PipelineRunManager()
    manager.es = fake_es
    manager.redis = fake_redis
    return manager, fake_es, fake_redis


def make_run(manager, issue_number=42, board='SDLC Execution', project='proj'):
    return manager.create_pipeline_run(
        issue_number=issue_number,
        issue_title='t',
        issue_url='u',
        project=project,
        board=board,
    )


def expire_record(manager, run):
    """Simulate ACTIVE_RUN_REDIS_TTL_SECONDS elapsing on the run's record.

    The issue mapping entry is a hash field and has no TTL of its own, so it
    survives -- which is the precondition this whole fix is about.
    """
    del manager.redis.data[manager._get_redis_key(run.id)]


def mapping(manager):
    return manager.redis.data.get(manager.redis_issue_mapping, {})


class TestExpiredMappingSurvivesWithoutEvidence:
    """Redis silence alone must never cost the mapping entry."""

    def test_survives_when_es_holds_no_document_for_the_run(self):
        manager, fake_es, _ = make_manager()
        run = make_run(manager)
        expire_record(manager, run)

        found = manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        assert found is None
        assert mapping(manager).get('proj:SDLC Execution:42') == run.id, (
            "the mapping entry -- the last record that this run exists -- was "
            "deleted on an empty Elasticsearch answer"
        )
        # Proves the reap actually ran and asked ES about THIS run, rather than
        # the assertion above passing because the branch was never reached.
        assert any(
            clause.get('term', {}).get('_id') == run.id
            for body in fake_es.searches
            for clause in body['query'].get('bool', {}).get('must', [])
        ), f"no by-id lookup for the expired run was issued: {fake_es.searches}"

    def test_survives_when_elasticsearch_is_unreachable(self):
        manager, fake_es, _ = make_manager(es=FakeElasticsearch(fail_search=True))
        run = make_run(manager)
        expire_record(manager, run)

        found = manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        assert found is None
        assert fake_es.searches, "ES was never consulted before deciding"
        assert mapping(manager).get('proj:SDLC Execution:42') == run.id, (
            "an Elasticsearch outage was read as evidence the run ended"
        )

    def test_survives_when_there_is_no_es_client_at_all(self):
        manager, _, _ = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        manager.es = None

        assert manager.get_active_pipeline_run('proj', 42, board='SDLC Execution') is None
        assert mapping(manager).get('proj:SDLC Execution:42') == run.id

    def test_survives_when_es_still_reports_the_run_active(self):
        """The #233 population: a live run whose record aged out of Redis."""
        manager, fake_es, _ = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        fake_es.seed(run.to_dict())

        found = manager.get_active_pipeline_run(
            'proj', 42, board='SDLC Execution', restore_to_redis=False
        )

        assert found is not None and found.id == run.id
        assert mapping(manager).get('proj:SDLC Execution:42') == run.id, (
            "a still-active run lost its mapping entry to a read-only lookup"
        )

    def test_survives_when_es_reports_it_feedback_listening(self):
        manager, fake_es, _ = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        doc = run.to_dict()
        doc['status'] = 'feedback_listening'
        fake_es.seed(doc)

        manager.get_active_pipeline_run(
            'proj', 42, board='SDLC Execution', restore_to_redis=False
        )

        assert mapping(manager).get('proj:SDLC Execution:42') == run.id

    def test_survives_when_the_es_document_is_unparseable(self):
        manager, fake_es, _ = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        fake_es.seed({'id': run.id, 'status': 'completed'})  # missing required fields

        manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        assert mapping(manager).get('proj:SDLC Execution:42') == run.id, (
            "a document too damaged to interpret was treated as proof the run ended"
        )


class TestExpiredMappingIsReapedOnPositiveEvidence:
    """The other half: evidence the run ended does license the delete."""

    def test_removed_when_es_reports_the_run_completed(self):
        manager, fake_es, _ = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        doc = run.to_dict()
        doc['status'] = 'completed'
        doc['ended_at'] = '2026-01-01T00:00:00Z'
        fake_es.seed(doc)

        assert manager.get_active_pipeline_run('proj', 42, board='SDLC Execution') is None
        assert 'proj:SDLC Execution:42' not in mapping(manager), (
            "a mapping for a run Elasticsearch says finished was left behind"
        )

    def test_removed_when_the_status_is_terminal_without_an_end_time(self):
        """A terminal status is evidence on its own; ended_at can be missing
        (an end that was interrupted between the two writes)."""
        manager, fake_es, _ = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        doc = run.to_dict()
        doc['status'] = 'completed'  # ended_at still None
        fake_es.seed(doc)

        manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        assert 'proj:SDLC Execution:42' not in mapping(manager)

    def test_both_keys_for_one_run_cost_a_single_es_lookup(self):
        """The board-scoped and legacy keys can hold the same run id (the ES
        restore path backfills the legacy one). Its state is one answer, so it
        is asked for once and both entries are reaped."""
        manager, fake_es, fake_redis = make_manager()
        run = make_run(manager)
        fake_redis.hset(manager.redis_issue_mapping, 'proj:42', run.id)
        expire_record(manager, run)
        doc = run.to_dict()
        doc['status'] = 'completed'
        fake_es.seed(doc)

        manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        by_id_lookups = [
            body for body in fake_es.searches
            if any(
                clause.get('term', {}).get('_id') == run.id
                for clause in body['query'].get('bool', {}).get('must', [])
            )
        ]
        assert len(by_id_lookups) == 1, (
            f"the same run's state was asked for {len(by_id_lookups)} times"
        )
        assert 'proj:SDLC Execution:42' not in mapping(manager)
        assert 'proj:42' not in mapping(manager)

    def test_only_the_expired_run_s_own_entry_is_touched(self):
        manager, fake_es, _ = make_manager()
        gone = make_run(manager, issue_number=42)
        other = make_run(manager, issue_number=77)
        expire_record(manager, gone)
        doc = gone.to_dict()
        doc['status'] = 'completed'
        doc['ended_at'] = '2026-01-01T00:00:00Z'
        fake_es.seed(doc)

        manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        assert mapping(manager).get('proj:SDLC Execution:77') == other.id


class TestReapUsesCompareAndDelete:
    """A new run can claim the same hash field between the read and the delete.

    FakeRedis.eval() executes the script text it is handed rather than
    emulating its effect, so these cover the shipped Lua as well as the call
    the reap makes.
    """

    def test_the_reap_evals_the_shared_script_against_the_run_it_expired(self):
        manager, fake_es, fake_redis = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        doc = run.to_dict()
        doc['status'] = 'completed'
        fake_es.seed(doc)

        manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        assert fake_redis.evals == [(
            _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT,
            1,
            manager.redis_issue_mapping,
            'proj:SDLC Execution:42',
            run.id,
        )], fake_redis.evals
        # The expected value is the run id READ from the mapping, not a re-read
        # of the field: a re-read would compare the value with itself and make
        # the compare vacuous.
        assert fake_redis.evals[0][-1] == run.id

    def test_the_script_spares_a_field_holding_a_different_value(self):
        """The Lua itself, exercised directly -- no orchestrator code involved."""
        fake_redis = FakeRedis()
        fake_redis.hset('h', 'f', 'newer-run')

        spared = fake_redis.eval(
            _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT, 1, 'h', 'f', 'older-run'
        )
        deleted = fake_redis.eval(
            _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT, 1, 'h', 'f', 'newer-run'
        )

        assert spared == 0 and deleted == 1
        assert fake_redis.hget('h', 'f') is None

    def test_the_script_is_lua_this_runner_actually_understands(self):
        """A script the runner cannot parse must fail loudly, not no-op."""
        with pytest.raises(LuaSubsetError):
            FakeRedis().eval("for i = 1, 10 do end", 0)

    def test_a_newer_run_written_into_the_same_field_is_spared(self):
        manager, fake_es, fake_redis = make_manager()
        run = make_run(manager)
        expire_record(manager, run)
        doc = run.to_dict()
        doc['status'] = 'completed'
        doc['ended_at'] = '2026-01-01T00:00:00Z'
        fake_es.seed(doc)

        # A concurrent create_pipeline_run() lands ONCE, while the reap is
        # asking ES about the old run. Deliberately not re-applied on later
        # searches: a callback that fired on every search would silently
        # re-create the entry after a bare HDEL had removed it, and the test
        # would pass against the very mistake it exists to catch.
        claimed = []

        def claim_the_field(body):
            if claimed:
                return
            claimed.append(True)
            fake_redis.hset(
                manager.redis_issue_mapping, 'proj:SDLC Execution:42', 'brand-new-run'
            )

        fake_es.on_search = claim_the_field

        manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        assert claimed, "the reap never reached its ES lookup"
        assert mapping(manager).get('proj:SDLC Execution:42') == 'brand-new-run', (
            "the reap deleted a newer run's mapping while cleaning up an older "
            "run's -- an unconditional HDEL instead of compare-and-delete"
        )


class TestStartupSweepStillSeesTheRun:
    """End to end, the reason #239 matters (#233).

    A run older than the record TTL: its Redis record expires, some routine
    lookup happens (a watchdog pass, any caller), then the run writes its record
    back -- _persist_run_update(), via update_resolved_workspace() or
    update_context_dir(), rewrites the record ONLY, never the mapping. (Not
    update_run_status(): it looks the run up first and returns False without
    writing anything when the lookup comes back empty, which is precisely the
    state below.) If the lookup deleted the mapping entry, nothing re-creates
    it, and get_active_run_workspaces() -- which iterates the mapping to find
    run ids before reading their records -- can no longer see the run at all.
    The startup sweep then prunes a live run's epic worktree.
    """

    def test_worktree_protection_survives_a_lookup_during_the_expiry_window(self):
        manager, fake_es, _ = make_manager()
        run = make_run(manager)
        run.project_dir = '/workspace/proj/worktrees/proj/1016'
        run.epic_id = '1016'
        manager.update_resolved_workspace(run)

        # The record's seven days elapse under the still-running run. ES no
        # longer holds its document either (past its retention window).
        expire_record(manager, run)

        # Any routine lookup during that window.
        manager.get_active_pipeline_run('proj', 42, board='SDLC Execution')

        # The run reaches its next stage and writes its record back through
        # _persist_run_update() -- record only; nothing here re-creates the
        # issue mapping entry.
        manager.update_resolved_workspace(run)

        workspaces = manager.get_active_run_workspaces()

        assert workspaces.complete is True
        assert workspaces.protects('proj', '/workspace/proj/worktrees/proj/1016'), (
            "a live run's epic worktree lost its protection because a routine "
            "lookup deleted the mapping entry that indexes it (#233/#239)"
        )
        assert workspaces.epic_ids_by_project.get('proj') == {'1016'}
