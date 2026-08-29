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
            # end_pipeline_run resolves the active run via get_active_pipeline_run,
            # which (unmodified, out of #43's scope) reads the legacy 2-field key —
            # that no longer matches what create_pipeline_run just wrote (board-
            # scoped), so its Redis fast path misses here and it falls through to
            # its existing ES fallback, which still finds run_b correctly. This is
            # a real, observable side effect of board-scoping the write side while
            # leaving get_active_pipeline_run's read side untouched per the issue's
            # explicit scope — noted for the record, not a correctness break as
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
        # Exercises _cleanup_issue_mapping() directly: end_pipeline_run()
        # itself always resolves "the" active run via get_active_pipeline_run()
        # (legacy-key-based, out of #43's scope), so it can't be used to force
        # "end run_a specifically while the legacy key points to run_b" — the
        # compare-and-delete behavior this test targets is a property of the
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
        has a TOCTOU window: a concurrent writer (e.g. get_active_pipeline_run's
        ES-restore path, pointing the legacy key at a different board's run)
        could land between the check and the delete. _cleanup_issue_mapping must
        go through a single atomic EVAL for the legacy key, not two separate
        Redis calls — verified with a bare mock (no internal hget/hdel
        emulation inside eval(), unlike MockRedis) so any direct hget/hdel
        call on the legacy key is unambiguously attributable to the method
        under test, not to a mock's own implementation."""
        manager, _, _ = make_manager()
        bare_mock_redis = MagicMock()
        manager.redis = bare_mock_redis

        manager._cleanup_issue_mapping('proj', 9, 'BoardA', 'run-a-id')

        bare_mock_redis.eval.assert_called_once_with(
            manager._cleanup_issue_mapping.__globals__['_COMPARE_AND_DELETE_HASH_FIELD_SCRIPT'],
            1,
            manager.redis_issue_mapping,
            'proj:9',
            'run-a-id',
        )
        # The board-scoped key is deleted directly (no compare needed there —
        # only this run could ever hold it); the legacy key must NOT be
        # touched via a separate hget/hdel outside the atomic eval() above.
        bare_mock_redis.hdel.assert_called_once_with(manager.redis_issue_mapping, 'proj:BoardA:9')
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
