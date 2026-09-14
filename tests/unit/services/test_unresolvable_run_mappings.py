"""
Regression tests for #233: an issue->run mapping entry whose run record has
expired was skipped silently, so "no run owns this worktree" was reported with
full confidence about a run nobody could account for. The startup sweep deletes
directories on that answer.

The measurement that shaped the fix: on the reference deployment 9 of 13 mapping
entries had no Redis record, and Elasticsearch could not resolve a single one.
So the obvious repair -- mark the whole answer incomplete whenever a record is
missing -- would have aborted every sweep forever, turning the protection into a
permanent no-op that still looked like protection. Hence two changes rather than
one: the doubt is scoped to the project it applies to, and the rubble that
produces it is now actually collected.
"""

import os
import pytest
from unittest.mock import Mock

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.pipeline_run import (
    ACTIVE_RUN_REDIS_TTL_SECONDS,
    ActiveRunWorkspaces,
    PipelineRunManager,
)


def _run_blob(project="codetoreum", epic_id="1016", status="active", ended_at=None):
    """Built from the real dataclass, so a new required field breaks this
    loudly rather than making the blob silently unparseable -- which would make
    these tests pass through the exception path instead of the one under test."""
    import json
    from services.pipeline_run import PipelineRun
    run = PipelineRun(
        id='run-1', issue_number=1045, issue_title='Phase 1',
        issue_url='https://github.com/o/r/issues/1045', project=project,
        board='SDLC Execution', started_at='2026-09-13T13:50:12Z',
        status=status, ended_at=ended_at, epic_id=epic_id,
        project_dir=f'/workspace/.orchestrator/worktrees/{project}/{epic_id}',
    )
    return json.dumps(run.to_dict())


def _manager(redis_client, es_client=None):
    return PipelineRunManager(
        redis_client=redis_client,
        elasticsearch_client=es_client,
        manage_schema=False,
    )


def _es(active_hits=(), by_id=None):
    """An ES double. `by_id` answers the per-run lookups the fix added."""
    by_id = by_id or {}

    def _search(index=None, body=None, **kw):
        q = body.get('query', {})
        if 'terms' in q:                      # the active-runs sweep query
            return {'hits': {'hits': [{'_source': s} for s in active_hits]}}
        run_id = q.get('term', {}).get('id')  # the per-run resolution query
        hit = by_id.get(run_id)
        return {'hits': {'hits': [{'_source': hit}] if hit is not None else []}}

    es = Mock()
    es.search.side_effect = _search
    return es


class TestAnUnaccountableMappingEntryIsNotAnAnswer:
    """The defect: `if not raw: continue` recorded an unknown as a fact."""

    def test_a_run_neither_store_knows_marks_its_project_unresolved(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-gone'}
        redis_client.get.return_value = None

        result = _manager(redis_client, _es()).get_active_run_workspaces()

        assert result.unresolved_projects == {'codetoreum'}
        assert result.answers_for('codetoreum') is False

    def test_the_doubt_does_not_spread_to_other_projects(self):
        """The reason this is scoped rather than global: with 9 dangling
        pointers in production, a global abort would never prune anything."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'test-project:dev:500': 'run-gone'}
        redis_client.get.return_value = None

        result = _manager(redis_client, _es()).get_active_run_workspaces()

        assert result.answers_for('test-project') is False
        assert result.answers_for('codetoreum') is True
        assert result.complete is True

    def test_a_run_elasticsearch_can_account_for_is_not_unresolved(self):
        """The common case by far: the run ended and its record aged out of
        Redis. ES still holds it, so nothing is in doubt."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-done'}
        redis_client.get.return_value = None
        es = _es(by_id={'run-done': {'id': 'run-done', 'status': 'completed'}})

        result = _manager(redis_client, es).get_active_run_workspaces()

        assert result.unresolved_projects == set()
        assert result.answers_for('codetoreum') is True

    def test_a_healthy_mapping_leaves_nothing_unresolved(self):
        """Control: the field must not be permanently populated."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-1'}
        redis_client.get.return_value = _run_blob()

        result = _manager(redis_client, _es()).get_active_run_workspaces()

        assert result.unresolved_projects == set()
        assert result.protects('codetoreum', '/workspace/.orchestrator/worktrees/codetoreum/1016')

    def test_an_unreadable_elasticsearch_does_not_resolve_the_doubt(self):
        """"Could not ask" is not "the run ended"."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-gone'}
        redis_client.get.return_value = None
        es = Mock()
        es.search.side_effect = RuntimeError("elasticsearch down")

        result = _manager(redis_client, es).get_active_run_workspaces()

        assert result.answers_for('codetoreum') is False


class TestAnswersForGatesOnBothScopes:
    """`complete` and `unresolved_projects` fail at different scopes and both
    have to hold before a project's directories may be deleted."""

    def test_an_incomplete_whole_answer_blocks_every_project(self):
        w = ActiveRunWorkspaces.unknown()
        assert w.answers_for('codetoreum') is False
        assert w.answers_for('anything') is False

    def test_an_unresolved_project_blocks_only_itself(self):
        w = ActiveRunWorkspaces({}, set(), complete=True,
                                unresolved_projects={'codetoreum'})
        assert w.answers_for('codetoreum') is False
        assert w.answers_for('heimdall') is True

    def test_a_clean_answer_permits_pruning(self):
        """Control: answers_for must not be unconditionally False."""
        assert ActiveRunWorkspaces({}, set()).answers_for('codetoreum') is True

    def test_the_unresolved_set_is_copied_not_aliased(self):
        """The value type is frozen so `complete` cannot be edited after the
        fact; the new field must not be a back door around that."""
        caller_set = {'codetoreum'}
        w = ActiveRunWorkspaces({}, set(), unresolved_projects=caller_set)
        caller_set.add('heimdall')
        assert w.answers_for('heimdall') is True


class TestTheMappingRubbleIsActuallyCollected:
    """Without this the fix above would suppress pruning indefinitely: the
    production mapping held 9 pointers that neither store could account for."""

    def test_an_entry_whose_run_is_gone_everywhere_is_removed(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {'test-project:dev:500': 'run-gone'}
        redis_client.exists.return_value = False

        cleaned = _manager(redis_client, _es()).cleanup_expired_mappings()

        assert cleaned == 1
        redis_client.hdel.assert_called_once()

    def test_an_entry_elasticsearch_still_calls_active_is_kept(self):
        """The run this pointer exists to protect: record outlived Redis, but
        ES says it is still going."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-live'}
        redis_client.exists.return_value = False
        es = _es(by_id={'run-live': {'id': 'run-live', 'status': 'active', 'ended_at': None}})

        cleaned = _manager(redis_client, es).cleanup_expired_mappings()

        assert cleaned == 0
        redis_client.hdel.assert_not_called()

    def test_an_entry_with_a_live_redis_record_is_kept(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-1'}
        redis_client.exists.return_value = True

        assert _manager(redis_client, _es()).cleanup_expired_mappings() == 0
        redis_client.hdel.assert_not_called()

    def test_an_unreadable_elasticsearch_keeps_the_entry(self):
        """Fails CLOSED, opposite to the resolution path: this one deletes on
        its answer, so "could not ask" must not read as "not active"."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-?'}
        redis_client.exists.return_value = False
        es = Mock()
        es.search.side_effect = RuntimeError("elasticsearch down")

        assert _manager(redis_client, es).cleanup_expired_mappings() == 0
        redis_client.hdel.assert_not_called()

    def test_no_elasticsearch_client_keeps_the_entry(self):
        """es=None is reachable only when the ES constructor itself failed --
        passing None to __init__ builds a real client. Set it directly."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-?'}
        redis_client.exists.return_value = False
        manager = _manager(redis_client, _es())
        manager.es = None

        assert manager.cleanup_expired_mappings() == 0
        redis_client.hdel.assert_not_called()

    def test_a_failing_redis_does_not_raise(self):
        """Runs unattended on a scheduler."""
        redis_client = Mock()
        redis_client.hgetall.side_effect = RuntimeError("redis down")

        assert _manager(redis_client, _es()).cleanup_expired_mappings() == 0


class TestTheRecordOutlivesTheLongestAgent:
    """The root cause: an active run's Redis record expired at 2h while
    senior_software_engineer may legitimately run for 3h without writing, so a
    live run went invisible to the only check protecting its workspace."""

    def test_the_ttl_exceeds_the_longest_configured_agent_timeout(self):
        import yaml
        with open('/app/config/foundations/agents.yaml') as fh:
            agents = yaml.safe_load(fh)['agents']
        longest = max(a.get('timeout', 0) for a in agents.values())

        assert longest > 0, "could not read agent timeouts"
        assert ACTIVE_RUN_REDIS_TTL_SECONDS > longest, (
            f"an agent may run {longest}s without writing, but its run record "
            f"expires after {ACTIVE_RUN_REDIS_TTL_SECONDS}s -- a live run would "
            f"go invisible to get_active_run_workspaces() (#233)"
        )
