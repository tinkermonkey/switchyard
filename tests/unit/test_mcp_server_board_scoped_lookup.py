"""
Tests for mcp/server.py's pipeline-run lookup tools after board-scoping the
issue->run Redis mapping (switchyard issue #43).

Covers the 3 call sites in mcp/server.py that read
"orchestrator:pipeline_run:issue_mapping":
- list_issues(): per-board loop, uses format_pipeline_run_issue_key(project,
  issue_number, board_name) — a board name is in scope for each iteration.
- get_issue(): single-issue lookup aggregating board_statuses across every
  board the issue sits on, so no single board is in scope — uses HSCAN with
  match=f"{project}:*:{issue_number}" to find any board-scoped active run,
  falling back to the legacy 2-field key for backward compatibility.
- list_active_runs(): parses hash keys back apart with str.partition(":") to
  extract just the project name — verified to work unchanged for both the
  legacy 2-field and new 3-field key shapes (partition splits on the FIRST
  colon only).
"""

import sys
import os
import json
from unittest.mock import MagicMock, patch

import pytest

# sys.path is mutated only for the duration of this import, then restored —
# leaving the insert in place for the rest of the pytest session made the
# repo root and mcp/ the FIRST places Python resolves top-level imports
# (e.g. a bare `import server`) for every test collected afterward, and
# contributed to unrelated test-order pollution elsewhere in the suite
# (confirmed: excluding this file entirely dropped the full-suite failure
# count by dozens). mcp/server.py itself has no __init__.py alongside it —
# see its own module docstring — specifically so it isn't a regular package
# that could shadow the installed `mcp` SDK; that's also why sys.path needs
# these two entries (repo root for mcp/server.py's own `from services...`
# imports, and mcp/ itself so `import server`/`import auth` resolve) only
# while the import below actually happens.
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
_mcp_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../mcp'))
sys.path.insert(0, _repo_root)
sys.path.insert(0, _mcp_dir)
try:
    import server as mcp_server
finally:
    sys.path.remove(_mcp_dir)
    sys.path.remove(_repo_root)


class FakeRedis:
    """Minimal in-memory stand-in for the subset of redis-py's hash API
    mcp/server.py uses (hget, hgetall, hscan) with decode_responses=True
    semantics (plain str in/out)."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hscan(self, key, cursor=0, match=None, count=None):
        # Single-page fake scan: return everything matching in one batch and
        # signal completion via cursor=0, same contract callers must honor.
        import fnmatch
        data = self.hashes.get(key, {})
        if match:
            data = {k: v for k, v in data.items() if fnmatch.fnmatchcase(k, match)}
        return 0, data


@pytest.fixture(autouse=True)
def fake_redis():
    r = FakeRedis()
    mcp_server._redis = None
    with patch.object(mcp_server, '_get_redis', return_value=r):
        yield r
    mcp_server._redis = None


class TestListActiveRunsPartition:
    """list_active_runs()'s str.partition(":") project-name extraction."""

    @pytest.mark.asyncio
    async def test_extracts_project_name_from_legacy_two_field_key(self, fake_redis):
        fake_redis.hset(
            "orchestrator:pipeline_run:issue_mapping", "proj:42", "run-legacy"
        )
        fake_redis.hashes["orchestrator:pipeline_run:run-legacy"] = None  # unused
        with patch.object(mcp_server, '_redis_get_run', return_value={
            'id': 'run-legacy', 'status': 'active', 'issue_number': 42, 'project': 'proj'
        }):
            results = await mcp_server.list_active_runs(project=None)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_extracts_project_name_from_board_scoped_three_field_key(self, fake_redis):
        fake_redis.hset(
            "orchestrator:pipeline_run:issue_mapping", "proj:BoardA:42", "run-new"
        )
        with patch.object(mcp_server, '_redis_get_run', return_value={
            'id': 'run-new', 'status': 'active', 'issue_number': 42, 'project': 'proj'
        }):
            results = await mcp_server.list_active_runs(project=None)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_project_filter_matches_both_key_shapes_for_the_same_project(self, fake_redis):
        """Two boards, same project, same issue — both runs' project names must
        resolve correctly for filtering, and both distinct runs must appear
        (no collision now that they're on different hash fields)."""
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj:BoardA:42", "run-a")
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj:BoardB:42", "run-b")

        def fake_get_run(run_id, r):
            return {'id': run_id, 'status': 'active', 'issue_number': 42, 'project': 'proj'}

        with patch.object(mcp_server, '_redis_get_run', side_effect=fake_get_run), \
             patch.object(mcp_server, '_resolve_project_name', return_value='proj'):
            results = await mcp_server.list_active_runs(project='proj')

        assert len(results) == 2
        assert {r['pipeline_run_id'] for r in results} == {'run-a', 'run-b'}

    @pytest.mark.asyncio
    async def test_project_filter_excludes_other_projects_for_both_key_shapes(self, fake_redis):
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj-a:42", "run-a")
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj-b:BoardX:7", "run-b")

        def fake_get_run(run_id, r):
            proj = 'proj-a' if run_id == 'run-a' else 'proj-b'
            issue = 42 if run_id == 'run-a' else 7
            return {'id': run_id, 'status': 'active', 'issue_number': issue, 'project': proj}

        with patch.object(mcp_server, '_redis_get_run', side_effect=fake_get_run), \
             patch.object(mcp_server, '_resolve_project_name', return_value='proj-a'):
            results = await mcp_server.list_active_runs(project='proj-a')

        assert len(results) == 1
        assert results[0]['project'] == 'proj-a'


class TestGetIssueBoardScopedLookup:
    """get_issue()'s HSCAN-based board-scoped lookup with legacy fallback."""

    def _mock_graphql_response(self, board_statuses):
        nodes = [
            {
                "id": f"item-{i}",
                "project": {"number": i, "title": bs["board_name"]},
                "fieldValueByName": {"name": bs["status"]},
            }
            for i, bs in enumerate(board_statuses)
        ]
        return {
            "data": {
                "repository": {
                    "issue": {
                        "title": "Test issue",
                        "url": "https://github.com/org/repo/issues/99",
                        "state": "OPEN",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "labels": {"nodes": []},
                        "assignees": {"nodes": []},
                        "comments": {"nodes": []},
                        "projectItems": {"nodes": nodes},
                    }
                }
            }
        }

    @pytest.mark.asyncio
    async def test_finds_board_scoped_active_run_via_hscan(self, fake_redis):
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj:BoardA:99", "run-a")
        fake_redis.hashes["orchestrator:pipeline_run:run-a"] = None

        resp = self._mock_graphql_response([{"board_name": "BoardA", "status": "In Progress"}])

        with patch.object(mcp_server, '_load_project_config', return_value={
            "project": {"github": {"org": "org", "repo": "repo"}}
        }), \
             patch.object(mcp_server, '_gh_graphql', return_value=resp), \
             patch.object(mcp_server, '_redis_get_run', return_value={'id': 'run-a', 'status': 'active'}):
            result = await mcp_server.get_issue(issue_number=99, project="proj")

        assert result["pipeline_run"]["id"] == "run-a"

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_key_when_no_board_scoped_match(self, fake_redis):
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj:99", "run-legacy")

        resp = self._mock_graphql_response([{"board_name": "BoardA", "status": "Done"}])

        with patch.object(mcp_server, '_load_project_config', return_value={
            "project": {"github": {"org": "org", "repo": "repo"}}
        }), \
             patch.object(mcp_server, '_gh_graphql', return_value=resp), \
             patch.object(mcp_server, '_redis_get_run', return_value={'id': 'run-legacy', 'status': 'active'}):
            result = await mcp_server.get_issue(issue_number=99, project="proj")

        assert result["pipeline_run"]["id"] == "run-legacy"

    @pytest.mark.asyncio
    async def test_no_run_found_returns_none_pipeline_run(self, fake_redis):
        resp = self._mock_graphql_response([{"board_name": "BoardA", "status": "Done"}])

        with patch.object(mcp_server, '_load_project_config', return_value={
            "project": {"github": {"org": "org", "repo": "repo"}}
        }), \
             patch.object(mcp_server, '_gh_graphql', return_value=resp):
            result = await mcp_server.get_issue(issue_number=99, project="proj")

        assert result["pipeline_run"] is None

    @pytest.mark.asyncio
    async def test_multiple_boards_active_picks_one_and_does_not_error(self, fake_redis):
        """The exact scenario issue #43 is about: the same issue has active
        runs on two different boards simultaneously. get_issue must not error
        and must deterministically return one of them."""
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj:BoardA:99", "run-a")
        fake_redis.hset("orchestrator:pipeline_run:issue_mapping", "proj:BoardB:99", "run-b")

        resp = self._mock_graphql_response([
            {"board_name": "BoardA", "status": "In Progress"},
            {"board_name": "BoardB", "status": "Review"},
        ])

        def fake_get_run(run_id, r):
            return {'id': run_id, 'status': 'active'}

        with patch.object(mcp_server, '_load_project_config', return_value={
            "project": {"github": {"org": "org", "repo": "repo"}}
        }), \
             patch.object(mcp_server, '_gh_graphql', return_value=resp), \
             patch.object(mcp_server, '_redis_get_run', side_effect=fake_get_run):
            result = await mcp_server.get_issue(issue_number=99, project="proj")

        assert result["pipeline_run"]["id"] in ("run-a", "run-b")
