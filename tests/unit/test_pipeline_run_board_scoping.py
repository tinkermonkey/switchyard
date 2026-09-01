"""
Tests for board-scoped pipeline-run issue keys (switchyard issue #43).

GitHub allows the same issue to sit on more than one board simultaneously.
Before this change, PipelineRunManager._get_issue_key(project, issue_number)
had no board component, so an active run on board A and a failed/retained
lock for the same issue on board B collided on the same Redis hash field.

Covers:
- format_pipeline_run_issue_key()'s board-scoped vs. legacy 2-field format.
- get_recent_pipeline_run_id()'s new optional `board` parameter: scoped
  lookup (Redis + ES) when given, unchanged legacy behavior when omitted.
- Two boards concurrently holding runs for the same (project, issue_number)
  are stored under independent Redis hash fields and do not collide.
"""

import json
from unittest.mock import MagicMock, patch

from services.pipeline_run import format_pipeline_run_issue_key, PipelineRunManager


class MockElasticsearch:
    """Mock Elasticsearch client whose search() actually filters indexed docs,
    so ES-fallback board-scoping can be verified end to end."""

    def __init__(self):
        self.indexed_docs = []
        self.docs_by_id = {}
        self.ilm = MagicMock()
        self.indices = MagicMock()
        self.indices.exists.return_value = False

    def index(self, index, id=None, document=None, body=None):
        doc = document or body
        self.indexed_docs.append({'index': index, 'id': id, 'body': doc})
        self.docs_by_id[id] = doc
        return {'result': 'created'}

    def search(self, index, body):
        must = body['query']['bool']['must']

        def matches(doc):
            for clause in must:
                if 'term' in clause:
                    (k, v), = clause['term'].items()
                    if doc.get(k) != v:
                        return False
                if 'terms' in clause:
                    (k, v), = clause['terms'].items()
                    if doc.get(k) not in v:
                        return False
            return True

        hits = [d for d in self.docs_by_id.values() if matches(d)]
        hits.sort(key=lambda d: d.get('started_at', ''), reverse=True)
        size = body.get('size', 10)
        return {
            'hits': {
                'total': {'value': len(hits)},
                'hits': [{'_source': h} for h in hits[:size]],
            }
        }


class MockRedis:
    """Mock Redis client for testing"""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value
        return True

    def hget(self, key, field):
        if key in self.data and isinstance(self.data[key], dict):
            return self.data[key].get(field)
        return None

    def hset(self, key, field, value):
        if key not in self.data or not isinstance(self.data[key], dict):
            self.data[key] = {}
        self.data[key][field] = value
        return 1

    def hdel(self, key, field):
        if key in self.data and isinstance(self.data[key], dict):
            self.data[key].pop(field, None)
        return 1

    def eval(self, script, numkeys, key, field, expected_value):
        """Emulate _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT against this
        in-memory store (a real Redis server runs the Lua script itself)."""
        if self.hget(key, field) == expected_value:
            return self.hdel(key, field)
        return 0

    def delete(self, key):
        if key in self.data:
            del self.data[key]
        return 1

    def exists(self, key):
        return key in self.data

    def lock(self, name, timeout=None, blocking_timeout=None):
        """No-op lock context manager — real Redis.lock() serializes concurrent
        get_or_create_pipeline_run() calls; single-threaded tests don't need
        that serialization, just something usable in a `with ...:` block."""
        return _NoopLock()


class _NoopLock:
    def __enter__(self):
        return True

    def __exit__(self, *exc_info):
        return False


def make_manager():
    mock_es = MockElasticsearch()
    mock_redis = MockRedis()
    with patch('services.pipeline_run.Elasticsearch', return_value=mock_es), \
         patch('services.pipeline_run.redis.Redis', return_value=mock_redis):
        manager = PipelineRunManager()
    manager.es = mock_es
    manager.redis = mock_redis
    return manager, mock_es, mock_redis


class TestFormatPipelineRunIssueKey:
    """The shared module-level helper used by pipeline_run.py, mcp/server.py,
    and observability_server.py."""

    def test_legacy_two_field_format_when_board_omitted(self):
        assert format_pipeline_run_issue_key('proj', 42) == 'proj:42'

    def test_legacy_two_field_format_when_board_is_none(self):
        assert format_pipeline_run_issue_key('proj', 42, None) == 'proj:42'

    def test_legacy_two_field_format_when_board_is_empty_string(self):
        assert format_pipeline_run_issue_key('proj', 42, '') == 'proj:42'

    def test_three_field_format_when_board_given(self):
        assert format_pipeline_run_issue_key('proj', 42, 'BoardA') == 'proj:BoardA:42'

    def test_manager_get_issue_key_delegates_to_module_function(self):
        manager, _, _ = make_manager()
        assert manager._get_issue_key('proj', 42) == 'proj:42'
        assert manager._get_issue_key('proj', 42, 'BoardA') == 'proj:BoardA:42'


class TestTwoBoardsSameIssueDoNotCollide:
    """The core scenario from issue #43: the same (project, issue_number)
    active on two different boards must not collide in the Redis mapping."""

    def test_create_pipeline_run_stores_independent_keys_per_board(self):
        manager, _, mock_redis = make_manager()

        run_a = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        run_b = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardB',
        )

        mapping = mock_redis.data[manager.redis_issue_mapping]
        assert mapping['proj:BoardA:5'] == run_a.id
        assert mapping['proj:BoardB:5'] == run_b.id
        assert run_a.id != run_b.id

    def test_get_recent_pipeline_run_id_with_board_resolves_independently(self):
        manager, _, _ = make_manager()

        run_a = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        run_b = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardB',
        )

        assert manager.get_recent_pipeline_run_id('proj', 5, board='BoardA') == run_a.id
        assert manager.get_recent_pipeline_run_id('proj', 5, board='BoardB') == run_b.id

    def test_end_pipeline_run_on_one_board_leaves_the_others_mapping_intact(self):
        manager, _, mock_redis = make_manager()

        manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        run_b = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardB',
        )

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_lm, \
             patch('subprocess.run') as mock_subprocess:
            mock_lm.return_value.release_lock = MagicMock()
            mock_subprocess.return_value = MagicMock(returncode=0, stdout='[]')
            # This call deliberately omits board=, so end_pipeline_run resolves the
            # active run via get_active_pipeline_run()'s legacy 2-field key (its
            # default when no board is passed) — that no longer matches what
            # create_pipeline_run just wrote (board-scoped), so the Redis fast path
            # misses here and it falls through to the existing ES fallback, which
            # still finds run_b correctly. This is a real, observable side effect of
            # calling end_pipeline_run() without board — not a correctness break as
            # long as ES is reachable (see PipelineRunManager module docstring).
            manager.end_pipeline_run('proj', 5, reason='done')

        mapping = mock_redis.data[manager.redis_issue_mapping]
        # BoardA's own mapping is untouched by BoardB's completion.
        assert mapping.get('proj:BoardA:5') is not None
        # BoardB's board-scoped mapping was removed by end_pipeline_run.
        assert 'proj:BoardB:5' not in mapping


class TestLegacyKeyCleanupOnEnd:
    """Regression tests for the review finding that end_pipeline_run() only
    deleted the new board-scoped key, orphaning any legacy 2-field key that
    get_active_pipeline_run()'s ES-fallback restore path may have written —
    a stale completed run's ID would otherwise linger under the legacy key
    forever, since nothing else ever cleans it up."""

    def test_end_pipeline_run_removes_legacy_key_if_it_points_to_this_run(self):
        manager, _, mock_redis = make_manager()

        run = manager.create_pipeline_run(
            issue_number=9, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        # Simulate get_active_pipeline_run()'s ES-fallback restore writing the
        # legacy (board-less) key for this same run.
        mock_redis.hset(manager.redis_issue_mapping, 'proj:9', run.id)

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_lm, \
             patch('subprocess.run') as mock_subprocess:
            mock_lm.return_value.release_lock = MagicMock()
            mock_subprocess.return_value = MagicMock(returncode=0, stdout='[]')
            manager.end_pipeline_run('proj', 9, reason='done')

        mapping = mock_redis.data[manager.redis_issue_mapping]
        assert 'proj:BoardA:9' not in mapping
        assert 'proj:9' not in mapping  # legacy key cleaned up too

    def test_cleanup_does_not_delete_legacy_key_owned_by_another_run(self):
        # Exercises _cleanup_issue_mapping() directly: end_pipeline_run() itself
        # resolves "the" active run via get_active_pipeline_run() — board-scoped
        # first if board is passed, else the legacy key — so it can't be used to
        # force "end run_a specifically while the legacy key points to run_b" —
        # the compare-and-delete behavior this test targets is a property of the
        # cleanup helper itself, tested in isolation here.
        manager, _, mock_redis = make_manager()

        run_a = manager.create_pipeline_run(
            issue_number=9, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        run_b = manager.create_pipeline_run(
            issue_number=9, issue_title='t', issue_url='u',
            project='proj', board='BoardB',
        )
        # Legacy key currently belongs to run_b (e.g. BoardB's run was the one
        # most recently restored into it via ES fallback).
        mock_redis.hset(manager.redis_issue_mapping, 'proj:9', run_b.id)

        # Clean up run_a's mapping — must NOT clobber the legacy key, since it
        # currently points to run_b, not run_a.
        manager._cleanup_issue_mapping('proj', 9, 'BoardA', run_a.id)

        mapping = mock_redis.data[manager.redis_issue_mapping]
        assert 'proj:BoardA:9' not in mapping  # run_a's own key is removed
        assert mapping.get('proj:9') == run_b.id  # legacy key untouched
        assert mapping.get('proj:BoardB:9') == run_b.id  # run_b unaffected

    def test_cleanup_uses_atomic_compare_and_delete_not_separate_check_then_act(self):
        """Regression test for the review finding that a separate HGET-then-HDEL
        has a TOCTOU window: a concurrent writer (e.g. a new run for the same
        board+issue, or get_active_pipeline_run's ES-restore path pointing the
        legacy key at a different board's run) could land between the check
        and the delete. _cleanup_issue_mapping must go through a single atomic
        EVAL for BOTH the board-scoped and legacy keys, never a separate
        hget()+hdel() pair — verified with a bare mock (no internal hget/hdel
        emulation inside eval(), unlike MockRedis) so any direct hget/hdel
        call is unambiguously attributable to the method under test, not to a
        mock's own implementation."""
        manager, _, _ = make_manager()
        bare_mock_redis = MagicMock()
        manager.redis = bare_mock_redis

        manager._cleanup_issue_mapping('proj', 9, 'BoardA', 'run-a-id')

        script = manager._cleanup_issue_mapping.__globals__['_COMPARE_AND_DELETE_HASH_FIELD_SCRIPT']
        assert bare_mock_redis.eval.call_args_list == [
            ((script, 1, manager.redis_issue_mapping, 'proj:BoardA:9', 'run-a-id'),),
            ((script, 1, manager.redis_issue_mapping, 'proj:9', 'run-a-id'),),
        ]
        # Neither key is ever touched via a separate hget/hdel outside the
        # atomic eval() calls above.
        bare_mock_redis.hdel.assert_not_called()
        bare_mock_redis.hget.assert_not_called()


class TestGetRecentPipelineRunIdBoardParameter:
    def test_board_none_keeps_legacy_two_field_redis_lookup(self):
        manager, _, mock_redis = make_manager()
        mock_redis.hset(manager.redis_issue_mapping, 'proj:7', 'run-legacy')
        assert manager.get_recent_pipeline_run_id('proj', 7) == 'run-legacy'

    def test_board_none_es_query_has_no_board_term(self):
        manager, mock_es, _ = make_manager()
        with patch.object(mock_es, 'search', wraps=mock_es.search) as spy:
            manager.get_recent_pipeline_run_id('proj', 7)
            body = spy.call_args.kwargs['body']
            terms = [list(c.get('term', {}).keys())[0] for c in body['query']['bool']['must'] if 'term' in c]
            assert 'board' not in terms

    def test_board_given_es_query_includes_board_term(self):
        manager, mock_es, _ = make_manager()
        with patch.object(mock_es, 'search', wraps=mock_es.search) as spy:
            manager.get_recent_pipeline_run_id('proj', 7, board='BoardA')
            body = spy.call_args.kwargs['body']
            terms = {list(c['term'].keys())[0]: list(c['term'].values())[0]
                     for c in body['query']['bool']['must'] if 'term' in c}
            assert terms.get('board') == 'BoardA'

    def test_board_given_falls_back_to_es_when_not_in_redis(self):
        manager, mock_es, _ = make_manager()
        run = manager.create_pipeline_run(
            issue_number=9, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        # Simulate the Redis mapping having expired/been removed, ES still has it.
        del manager.redis.data[manager.redis_issue_mapping]['proj:BoardA:9']
        found = manager.get_recent_pipeline_run_id('proj', 9, board='BoardA')
        assert found == run.id


class TestGetActivePipelineRunBoardParameter:
    """get_active_pipeline_run(board=...) — the read-side half of this PR's fix.
    create_pipeline_run() always writes the board-scoped key; these verify the
    read side now actually finds it."""

    def test_board_scoped_key_takes_precedence_over_legacy(self):
        manager, _, mock_redis = make_manager()
        run_a = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        # A stale legacy entry pointing at a DIFFERENT run for the same issue.
        run_b = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardB',
        )
        mock_redis.hset(manager.redis_issue_mapping, 'proj:5', run_b.id)

        found = manager.get_active_pipeline_run('proj', 5, board='BoardA')
        assert found is not None
        assert found.id == run_a.id

    def test_board_none_checks_only_the_legacy_redis_key_unchanged(self):
        manager, _, mock_redis = make_manager()
        manager.es = None  # isolate the Redis-only lookup path
        manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        # Board-scoped key exists, but board=None must not check it — only the
        # legacy key, which was never written for this issue.
        assert manager.get_active_pipeline_run('proj', 5) is None

    def test_board_none_still_finds_via_unfiltered_es_fallback_unchanged(self):
        """board=None's ES fallback stays unfiltered (matches by project +
        issue_number only) — exactly the pre-PR behavior, just now reachable
        even when a board-scoped-only run exists."""
        manager, _, mock_redis = make_manager()
        run = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        found = manager.get_active_pipeline_run('proj', 5)
        assert found is not None
        assert found.id == run.id

    def test_falls_back_to_legacy_key_when_board_scoped_key_absent(self):
        manager, _, mock_redis = make_manager()
        run = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        # Simulate a run only ever restored under the legacy key (e.g. by a
        # board=None caller's ES-fallback restore) — board-scoped key absent.
        del mock_redis.data[manager.redis_issue_mapping]['proj:BoardA:5']
        mock_redis.hset(manager.redis_issue_mapping, 'proj:5', run.id)

        found = manager.get_active_pipeline_run('proj', 5, board='BoardA')
        assert found is not None
        assert found.id == run.id

    def test_legacy_key_owned_by_a_different_board_is_not_adopted(self):
        """The legacy key is shared across boards; unlike the board-scoped key
        it can point at a genuinely different board's active run. Adopting it
        would misdirect end_pipeline_run()'s downstream lock release/queue
        processing (both keyed off pipeline_run.board, not the requested board)."""
        manager, _, mock_redis = make_manager()
        run_b = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardB',
        )
        mock_redis.hset(manager.redis_issue_mapping, 'proj:5', run_b.id)

        found = manager.get_active_pipeline_run('proj', 5, board='BoardA')
        assert found is None

    def test_es_fallback_restores_under_board_scoped_key_when_board_given(self):
        manager, mock_es, mock_redis = make_manager()
        run = manager.create_pipeline_run(
            issue_number=9, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        # Simulate the Redis mapping having been lost (e.g. TTL expiry) while
        # ES still has the doc.
        del mock_redis.data[manager.redis_issue_mapping]['proj:BoardA:9']

        found = manager.get_active_pipeline_run('proj', 9, board='BoardA')
        assert found is not None
        assert found.id == run.id
        mapping = mock_redis.data[manager.redis_issue_mapping]
        assert mapping.get('proj:BoardA:9') == run.id
        assert 'proj:9' not in mapping


class TestGetOrCreatePipelineRunReusesFreshRun:
    """Regression test for the phantom-duplicate-run bug this PR fixes: a run
    created moments earlier for a given (project, board, issue_number) must be
    reused by a second, same-second get_or_create_pipeline_run() call for the
    same key — not silently duplicated because the read side only checked the
    legacy, board-less mapping while the write side always wrote board-scoped.

    This mirrors the real trigger_agent_for_status() -> _start_pr_review_for_issue()
    sequence: both eagerly call get_or_create_pipeline_run() for the same
    (project, board, issue_number) moments apart.
    """

    def test_second_call_reuses_run_created_by_first_call(self):
        manager, _, mock_redis = make_manager()

        first_run, first_created = manager.get_or_create_pipeline_run(
            issue_number=277, issue_title='P4: Proactive check-ins', issue_url='u',
            project='phone-home', board='Planning & Design',
        )
        assert first_created is True

        second_run, second_created = manager.get_or_create_pipeline_run(
            issue_number=277, issue_title='P4: Proactive check-ins', issue_url='u',
            project='phone-home', board='Planning & Design',
        )

        assert second_created is False
        assert second_run.id == first_run.id

        # Exactly one run was ever created for this issue — not a phantom plus
        # a real one.
        run_data_keys = [
            k for k in mock_redis.data
            if k.startswith(f"{manager.redis_prefix}:") and k != manager.redis_issue_mapping
        ]
        assert len(run_data_keys) == 1

        mapping = mock_redis.data[manager.redis_issue_mapping]
        assert mapping['phone-home:Planning & Design:277'] == first_run.id

    def test_call_for_a_different_board_does_not_reuse_the_first_boards_run(self):
        manager, _, _ = make_manager()

        run_a, created_a = manager.get_or_create_pipeline_run(
            issue_number=277, issue_title='t', issue_url='u',
            project='phone-home', board='Planning & Design',
        )
        run_b, created_b = manager.get_or_create_pipeline_run(
            issue_number=277, issue_title='t', issue_url='u',
            project='phone-home', board='Development',
        )

        assert created_a is True
        assert created_b is True
        assert run_a.id != run_b.id


class TestEndPhantomPipelineRun:
    """end_phantom_pipeline_run() — ends a run by id directly, bypassing the
    (project, issue_number, board) mapping lookup end_pipeline_run() uses,
    since by cleanup time that mapping may already point at whichever run
    superseded the phantom."""

    def test_ends_an_active_run_and_marks_it_superseded(self):
        manager, _, mock_redis = make_manager()
        run = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )

        result = manager.end_phantom_pipeline_run(
            pipeline_run_id=run.id, project='proj', issue_number=5,
            reason='test cleanup',
        )

        assert result is True
        stored = json.loads(mock_redis.data[manager._get_redis_key(run.id)])
        assert stored['status'] == 'completed'
        assert stored['outcome'] == 'superseded'
        assert stored['ended_at'] is not None

    def test_returns_false_and_is_a_noop_for_unknown_id(self):
        manager, mock_es, mock_redis = make_manager()

        result = manager.end_phantom_pipeline_run(
            pipeline_run_id='does-not-exist', project='proj', issue_number=5,
            reason='test cleanup',
        )

        assert result is False
        assert mock_redis.data == {}
        assert mock_es.indexed_docs == []

    def test_returns_false_and_is_a_noop_for_already_terminal_run(self):
        manager, mock_es, mock_redis = make_manager()
        run = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        manager.end_pipeline_run('proj', 5, board='BoardA', reason='already done')
        mock_es.indexed_docs.clear()  # the end_pipeline_run() call above indexed it

        result = manager.end_phantom_pipeline_run(
            pipeline_run_id=run.id, project='proj', issue_number=5,
            reason='redundant cleanup attempt',
        )

        assert result is False
        assert mock_es.indexed_docs == []

    def test_does_not_clobber_a_different_runs_mapping_compare_and_delete(self):
        """The phantom (run_a) is superseded by run_b, which has already
        overwritten the board-scoped issue mapping. Ending run_a by id must
        leave run_b's mapping entry intact — this is exactly the scenario
        _start_pr_review_for_issue()'s defensive cleanup relies on."""
        manager, _, mock_redis = make_manager()
        run_a = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        run_b = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )
        # run_b's create_pipeline_run() call above already overwrote the mapping.
        assert mock_redis.data[manager.redis_issue_mapping]['proj:BoardA:5'] == run_b.id

        result = manager.end_phantom_pipeline_run(
            pipeline_run_id=run_a.id, project='proj', issue_number=5,
            reason='superseded',
        )

        assert result is True
        assert mock_redis.data[manager.redis_issue_mapping]['proj:BoardA:5'] == run_b.id

    def test_does_not_touch_the_pipeline_lock(self):
        """Unlike end_pipeline_run(), this must never call into the lock
        manager — the phantom and whatever superseded it share the same
        (project, board, issue_number) lock, which the real run still needs."""
        manager, _, _ = make_manager()
        run = manager.create_pipeline_run(
            issue_number=5, issue_title='t', issue_url='u',
            project='proj', board='BoardA',
        )

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_lm:
            manager.end_phantom_pipeline_run(
                pipeline_run_id=run.id, project='proj', issue_number=5,
                reason='test cleanup',
            )
            mock_lm.assert_not_called()
