"""
Unit tests for the chunked, aliased cross-project GraphQL board-query builder
in services/github_owner_utils.py (GitHub issue #92, sub-issue of #36).

Covers:
- build_batched_projects_v2_query(): single-owner aliased query construction
- build_batched_board_queries(): chunking (single-chunk happy path,
  multi-chunk splitting, multi-owner splitting)
- parse_batched_projects_v2_response(): happy-path parsing and partial-batch
  GraphQL error attribution
- execute_batched_board_queries(): end-to-end wiring over a mocked GitHub
  client, including a fully-failed batch
- Cache unification (GitHub issue #93): execute_batched_board_queries() and
  execute_board_query_cached() sharing one _board_query_cache - partial
  cache hits within a batch, cross-path reads of each other's writes, and
  invalidate_board_query_cache() invalidating a batch-populated entry.

No live network access - all GitHub API calls are mocked.
"""
import time

import pytest
from unittest.mock import Mock, patch

from services.github_owner_utils import (
    MAX_BOARDS_PER_BATCH,
    _board_query_cache,
    build_batched_board_queries,
    build_batched_projects_v2_query,
    execute_batched_board_queries,
    execute_board_query_cached,
    invalidate_board_query_cache,
    parse_batched_projects_v2_response,
)


@pytest.fixture(autouse=True)
def _clear_board_query_cache():
    """
    _board_query_cache is a module-level global shared by every test in this
    file (and by execute_board_query_cached()/execute_batched_board_queries()
    themselves). Without clearing it, a board cached by one test is a stale
    cache hit in the next test that reuses the same (owner, project_number)
    pair, bypassing that test's mocked GitHub client entirely.
    """
    _board_query_cache.clear()
    yield
    _board_query_cache.clear()


def _board_data(number: int) -> dict:
    """Minimal but shaped-like-real projectV2 data for board `number`."""
    return {
        'id': f'PVT_{number}',
        'title': f'Board {number}',
        'items': {'nodes': [{'id': f'item-{number}'}]},
    }


class TestBuildBatchedProjectsV2Query:
    def test_returns_none_for_empty_project_numbers(self):
        assert build_batched_projects_v2_query('acme', []) is None

    def test_returns_none_when_owner_type_unresolvable(self):
        with patch('services.github_owner_utils.get_owner_type', return_value=None):
            assert build_batched_projects_v2_query('acme', [1, 2]) is None

    def test_aliases_each_project_number_under_organization_root(self):
        with patch('services.github_owner_utils.get_owner_type', return_value='organization'):
            query = build_batched_projects_v2_query('acme', [1, 2, 3])

        assert query is not None
        assert 'organization(login: "acme")' in query
        assert 'user(login:' not in query
        for number in (1, 2, 3):
            assert f'p{number}: projectV2(number: {number})' in query
        # Shared field selection is present under every alias
        assert query.count('items(first: 100') == 3
        assert query.count('fieldValues(first: 10)') == 3

    def test_aliases_each_project_number_under_user_root(self):
        with patch('services.github_owner_utils.get_owner_type', return_value='user'):
            query = build_batched_projects_v2_query('octocat', [7])

        assert query is not None
        assert 'user(login: "octocat")' in query
        assert 'p7: projectV2(number: 7)' in query


class TestBuildBatchedBoardQueries:
    def test_single_chunk_happy_path(self):
        with patch('services.github_owner_utils.get_owner_type', return_value='organization'):
            batches = build_batched_board_queries([('acme', 1), ('acme', 2), ('acme', 3)])

        assert len(batches) == 1
        batch = batches[0]
        assert batch['owner'] == 'acme'
        assert batch['boards'] == [('acme', 1, 'p1'), ('acme', 2, 'p2'), ('acme', 3, 'p3')]
        assert 'p1: projectV2(number: 1)' in batch['query']
        assert 'p2: projectV2(number: 2)' in batch['query']
        assert 'p3: projectV2(number: 3)' in batch['query']

    def test_multi_chunk_splitting_never_exceeds_max_per_batch(self):
        project_numbers = list(range(1, MAX_BOARDS_PER_BATCH + 5))  # one full chunk + overflow
        pairs = [('acme', n) for n in project_numbers]

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'):
            batches = build_batched_board_queries(pairs)

        assert len(batches) == 2
        assert len(batches[0]['boards']) == MAX_BOARDS_PER_BATCH
        assert len(batches[1]['boards']) == 4
        for batch in batches:
            assert len(batch['boards']) <= MAX_BOARDS_PER_BATCH
            assert batch['owner'] == 'acme'

        # Every project number appears exactly once across all chunks
        all_numbers = [number for batch in batches for _owner, number, _alias in batch['boards']]
        assert sorted(all_numbers) == project_numbers

    def test_multi_owner_never_mixed_into_one_query(self):
        pairs = [('acme', 1), ('acme', 2), ('globex', 10), ('globex', 11), ('globex', 12)]

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'):
            batches = build_batched_board_queries(pairs)

        owners_seen = {batch['owner'] for batch in batches}
        assert owners_seen == {'acme', 'globex'}
        assert len(batches) == 2

        for batch in batches:
            board_owners = {owner for owner, _number, _alias in batch['boards']}
            assert board_owners == {batch['owner']}  # never mixed

        acme_batch = next(b for b in batches if b['owner'] == 'acme')
        globex_batch = next(b for b in batches if b['owner'] == 'globex')
        assert [n for _o, n, _a in acme_batch['boards']] == [1, 2]
        assert [n for _o, n, _a in globex_batch['boards']] == [10, 11, 12]

    def test_owner_boundary_starts_new_chunk_even_with_room_left(self):
        # Two boards for owner A (well under the cap), then one for owner B -
        # B must not be folded into A's chunk even though there's room.
        pairs = [('acme', 1), ('acme', 2), ('globex', 1)]

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'):
            batches = build_batched_board_queries(pairs)

        assert len(batches) == 2
        assert {b['owner'] for b in batches} == {'acme', 'globex'}

    def test_skips_owner_whose_query_fails_to_build(self):
        def fake_owner_type(owner_login):
            return None if owner_login == 'bad-owner' else 'organization'

        pairs = [('acme', 1), ('bad-owner', 99)]
        with patch('services.github_owner_utils.get_owner_type', side_effect=fake_owner_type):
            batches = build_batched_board_queries(pairs)

        assert len(batches) == 1
        assert batches[0]['owner'] == 'acme'


class TestParseBatchedProjectsV2Response:
    def test_happy_path_unwrapped_success_payload(self):
        boards = [('acme', 1, 'p1'), ('acme', 2, 'p2')]
        response = {
            'organization': {
                'p1': _board_data(1),
                'p2': _board_data(2),
            }
        }

        results, errors = parse_batched_projects_v2_response(response, boards)

        assert errors == {}
        assert results[('acme', 1)]['title'] == 'Board 1'
        assert results[('acme', 2)]['title'] == 'Board 2'
        assert results[('acme', 1)]['items']['nodes'] == [{'id': 'item-1'}]

    def test_happy_path_full_envelope_with_user_root(self):
        boards = [('octocat', 5, 'p5')]
        response = {
            'data': {'user': {'p5': _board_data(5)}},
        }

        results, errors = parse_batched_projects_v2_response(response, boards)

        assert errors == {}
        assert results == {('octocat', 5): _board_data(5)}

    def test_partial_batch_error_attributed_by_path_rest_still_parsed(self):
        boards = [('acme', 1, 'p1'), ('acme', 42, 'p42'), ('acme', 3, 'p3')]
        response = {
            'data': {
                'organization': {
                    'p1': _board_data(1),
                    'p42': None,
                    'p3': _board_data(3),
                }
            },
            'errors': [
                {
                    'type': 'NOT_FOUND',
                    'path': ['organization', 'p42'],
                    'message': "Could not resolve to a ProjectV2 with the number 42.",
                }
            ],
        }

        results, errors = parse_batched_projects_v2_response(response, boards)

        # The failing board is reported as an error, not silently dropped...
        assert ('acme', 42) in errors
        assert 'Could not resolve' in errors[('acme', 42)]
        assert ('acme', 42) not in results

        # ...and the rest of the batch still parses successfully.
        assert results[('acme', 1)]['title'] == 'Board 1'
        assert results[('acme', 3)]['title'] == 'Board 3'
        assert ('acme', 1) not in errors
        assert ('acme', 3) not in errors

    def test_multiple_errors_attributed_to_correct_distinct_boards(self):
        boards = [('acme', 1, 'p1'), ('acme', 2, 'p2'), ('acme', 3, 'p3')]
        response = {
            'data': {'organization': {'p1': _board_data(1), 'p2': None, 'p3': None}},
            'errors': [
                {'path': ['organization', 'p2'], 'message': 'p2 failed'},
                {'path': ['organization', 'p3'], 'message': 'p3 failed'},
            ],
        }

        results, errors = parse_batched_projects_v2_response(response, boards)

        assert set(errors.keys()) == {('acme', 2), ('acme', 3)}
        assert errors[('acme', 2)] == 'p2 failed'
        assert errors[('acme', 3)] == 'p3 failed'
        assert set(results.keys()) == {('acme', 1)}

    def test_missing_alias_without_explicit_error_is_reported_as_error(self):
        # Alias absent from data entirely, and no top-level error mentions it -
        # should still surface as a per-board error rather than being dropped.
        boards = [('acme', 1, 'p1'), ('acme', 2, 'p2')]
        response = {'organization': {'p1': _board_data(1)}}  # p2 missing

        results, errors = parse_batched_projects_v2_response(response, boards)

        assert results == {('acme', 1): _board_data(1)}
        assert ('acme', 2) in errors

    def test_empty_boards_list_returns_empty_results(self):
        assert parse_batched_projects_v2_response({'organization': {}}, []) == ({}, {})

    def test_none_response_fails_every_board(self):
        boards = [('acme', 1, 'p1'), ('acme', 2, 'p2')]

        results, errors = parse_batched_projects_v2_response(None, boards)

        assert results == {}
        assert set(errors.keys()) == {('acme', 1), ('acme', 2)}

    def test_unattributable_error_is_logged_and_does_not_crash(self):
        # An error whose path doesn't match any known alias must not blow up
        # parsing, and every board should still parse normally.
        boards = [('acme', 1, 'p1')]
        response = {
            'data': {'organization': {'p1': _board_data(1)}},
            'errors': [{'path': ['organization', 'someOtherField'], 'message': 'unrelated'}],
        }

        results, errors = parse_batched_projects_v2_response(response, boards)

        assert results == {('acme', 1): _board_data(1)}
        assert errors == {}


class TestExecuteBatchedBoardQueries:
    def test_executes_one_query_per_batch_and_merges_results(self):
        pairs = [('acme', 1), ('acme', 2), ('globex', 10)]

        mock_client = Mock()

        def fake_graphql(query):
            if 'organization(login: "acme")' in query:
                return True, {'organization': {'p1': _board_data(1), 'p2': _board_data(2)}}
            elif 'organization(login: "globex")' in query:
                return True, {'organization': {'p10': _board_data(10)}}
            raise AssertionError(f"Unexpected query: {query}")

        mock_client.graphql.side_effect = fake_graphql

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            results, errors = execute_batched_board_queries(pairs)

        assert mock_client.graphql.call_count == 2
        assert errors == {}
        assert set(results.keys()) == {('acme', 1), ('acme', 2), ('globex', 10)}
        assert results[('globex', 10)]['title'] == 'Board 10'

    def test_partial_failure_within_a_batch_is_surfaced_alongside_successes(self):
        pairs = [('acme', 1), ('acme', 42)]
        mock_client = Mock()
        mock_client.graphql.return_value = (
            False,
            {
                'data': {'organization': {'p1': _board_data(1), 'p42': None}},
                'errors': [{'path': ['organization', 'p42'], 'message': 'gone'}],
            },
        )

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            results, errors = execute_batched_board_queries(pairs)

        assert results == {('acme', 1): _board_data(1)}
        assert errors == {('acme', 42): 'gone'}

    def test_total_batch_failure_marks_every_board_in_it_as_errored(self):
        pairs = [('acme', 1), ('acme', 2)]
        mock_client = Mock()
        mock_client.graphql.return_value = (False, {"error": "rate_limited", "details": "..."})

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            results, errors = execute_batched_board_queries(pairs)

        assert results == {}
        assert set(errors.keys()) == {('acme', 1), ('acme', 2)}
        assert all(err == 'rate_limited' for err in errors.values())

    def test_no_batches_returns_empty_results(self):
        with patch('services.github_owner_utils.get_owner_type', return_value=None):
            results, errors = execute_batched_board_queries([('bad-owner', 1)])

        assert results == {}
        assert errors == {}


class TestUnifiedBoardQueryCache:
    """
    GitHub issue #93: execute_batched_board_queries() and
    execute_board_query_cached() must share exactly one cache
    (_board_query_cache), not two parallel ones.
    """

    def test_batched_call_partially_hits_cache_only_uncached_boards_fetched(self):
        """A board already cached and fresh is served without a network call;
        only the still-uncached boards end up in the query actually sent."""
        # Pre-populate the cache for ('acme', 1) directly, in the same
        # envelope shape execute_board_query_cached() stores.
        _board_query_cache[('acme', 1)] = (
            time.time(),
            {'organization': {'projectV2': _board_data(1)}},
        )

        mock_client = Mock()

        def fake_graphql(query):
            # Only board 2 should ever be queried - board 1 is a cache hit.
            assert 'p1: projectV2' not in query
            assert 'p2: projectV2(number: 2)' in query
            return True, {'organization': {'p2': _board_data(2)}}

        mock_client.graphql.side_effect = fake_graphql

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            results, errors = execute_batched_board_queries([('acme', 1), ('acme', 2)])

        assert errors == {}
        assert mock_client.graphql.call_count == 1  # only the uncached board fetched
        assert results[('acme', 1)]['title'] == 'Board 1'
        assert results[('acme', 2)]['title'] == 'Board 2'

    def test_batched_results_populate_cache_for_subsequent_single_board_call(self):
        """A board fetched via execute_batched_board_queries() is a cache
        hit for execute_board_query_cached() within the TTL - no new call."""
        mock_client = Mock()
        mock_client.graphql.return_value = (True, {'organization': {'p1': _board_data(1)}})

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            results, _errors = execute_batched_board_queries([('acme', 1)])
            assert mock_client.graphql.call_count == 1

            cached = execute_board_query_cached('acme', 1)

        assert mock_client.graphql.call_count == 1  # no new API call - served from cache
        assert cached == {'organization': {'projectV2': _board_data(1)}}
        assert results[('acme', 1)] == _board_data(1)

    def test_single_board_call_populates_cache_for_subsequent_batched_call(self):
        """The reverse direction: a board fetched via
        execute_board_query_cached() is a cache hit for
        execute_batched_board_queries() within the TTL - no new call."""
        mock_client = Mock()
        mock_client.graphql.return_value = (True, {'organization': {'projectV2': _board_data(1)}})

        with patch('services.github_owner_utils.build_projects_v2_query', return_value='query {}'), \
             patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            data = execute_board_query_cached('acme', 1)
            assert mock_client.graphql.call_count == 1

            results, errors = execute_batched_board_queries([('acme', 1)])

        assert mock_client.graphql.call_count == 1  # no new API call - served from cache
        assert errors == {}
        assert results == {('acme', 1): _board_data(1)}
        assert data == {'organization': {'projectV2': _board_data(1)}}

    def test_invalidate_clears_entry_populated_via_batched_path(self):
        """invalidate_board_query_cache() must correctly invalidate a board
        whose cache entry was populated by the batched path, not just ones
        populated by execute_board_query_cached()."""
        mock_client = Mock()
        mock_client.graphql.return_value = (True, {'organization': {'p1': _board_data(1)}})

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            execute_batched_board_queries([('acme', 1)])
            assert ('acme', 1) in _board_query_cache

            invalidate_board_query_cache('acme', 1)
            assert ('acme', 1) not in _board_query_cache

            # A subsequent fetch (either path) must hit the network again.
            execute_batched_board_queries([('acme', 1)])
            assert mock_client.graphql.call_count == 2

    def test_cache_write_uses_actual_response_root_key_not_a_second_get_owner_type_call(self):
        """
        Regression test: the cache-write step must derive the envelope's
        root key ('user' vs 'organization') from the actual GraphQL
        response payload, not from a second get_owner_type() call.

        get_owner_type() is independently cached/circuit-broken and can
        legitimately return a different result on a second call than it did
        when the query was built (e.g. a transient failure after Redis is
        unreachable). Re-deriving the root key from it risked caching a
        'user'-owned board's data under the 'organization' key, which
        execute_board_query_cached() callers key off exactly - a silent
        empty-items bug downstream. get_owner_type is intentionally NOT
        patched to return 'user' here even though the response is
        user-shaped, to prove the cache write no longer depends on it.
        """
        mock_client = Mock()
        # The response is 'user'-shaped, but get_owner_type() below will
        # report 'organization' - the fix must trust the response, not the
        # (now differing) get_owner_type() call.
        mock_client.graphql.return_value = (True, {'user': {'p1': _board_data(1)}})

        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):
            results, errors = execute_batched_board_queries([('acme', 1)])

        assert errors == {}
        assert results[('acme', 1)]['title'] == 'Board 1'
        # The cached envelope must be keyed 'user' (matching the actual
        # response), not 'organization' (what the second get_owner_type()
        # call would have said).
        assert _board_query_cache[('acme', 1)][1] == {'user': {'projectV2': _board_data(1)}}
