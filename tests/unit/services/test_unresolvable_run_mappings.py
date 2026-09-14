"""
Regression tests for #233: an issue->run mapping entry whose run record has
expired was skipped silently, so "no run owns this worktree" was reported with
full confidence about a run nobody could account for. The startup sweep deletes
directories on that answer.

The measurement that shaped the fix (reference deployment, 2026-09-14): 9 of 13
mapping entries had no Redis record, and Elasticsearch could not resolve one of
them -- mostly test-fixture residue rather than runs that ended and aged out.
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


class FakeRedis:
    """Enough Redis for the mapping cleanup, with real hash semantics.

    Mock() cannot express "hkeys returns what hset put there", which is what the
    age gate is built on -- and a Mock's non-iterable return silently became a
    swallowed warning rather than a failing test.
    """

    def __init__(self, mapping=None, records=None):
        self.hashes = {'orchestrator:pipeline_run:issue_mapping': dict(mapping or {})}
        self.records = dict(records or {})
        self.deleted = []
        self.fail_exists_for = set()
        #: Fires once, between the mapping read and the delete -- the only
        #: window a concurrent writer can repoint a field this pass already read.
        self.on_exists = None

    # -- strings -------------------------------------------------------
    def get(self, key):
        return self.records.get(key)

    def exists(self, key):
        if key in self.fail_exists_for:
            raise RuntimeError("redis blip")
        if self.on_exists is not None:
            hook, self.on_exists = self.on_exists, None
            hook(self)
        return 1 if key in self.records else 0

    def setex(self, key, ttl, value):
        self.records[key] = value

    # -- hashes --------------------------------------------------------
    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value
        return 1

    def hkeys(self, name):
        return list(self.hashes.get(name, {}))

    def hdel(self, name, field):
        return 1 if self.hashes.get(name, {}).pop(field, None) is not None else 0

    # -- the compare-and-delete lua the cleanup uses --------------------
    def eval(self, script, numkeys, name, field, expected):
        current = self.hashes.get(name, {}).get(field)
        if current != expected:
            return 0
        del self.hashes[name][field]
        self.deleted.append(field)
        return 1


def _manager(redis_client, es_client=None):
    return PipelineRunManager(
        redis_client=redis_client,
        elasticsearch_client=es_client,
        manage_schema=False,
    )


def _es(active_hits=(), by_id=None, fail_per_run=False):
    """An ES double that distinguishes the two queries this code makes.

    `fail_per_run` breaks ONLY the per-run term lookup. Failing every search
    instead makes the outer active-runs sweep raise first, which sets
    complete=False and short-circuits the per-run path entirely -- so an
    assertion on answers_for() would pass without the code under test ever
    running. That is how the original version of this double hid a surviving
    mutation in _elasticsearch_knows_run().
    """
    by_id = by_id or {}

    def _search(index=None, body=None, **kw):
        q = body.get('query', {})
        if 'terms' in q:                      # the active-runs sweep query
            return {'hits': {'hits': [{'_source': s} for s in active_hits]}}
        if fail_per_run:
            raise RuntimeError("elasticsearch down")
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
        """The reason this is scoped rather than global: with most mapping
        entries dangling, a global abort would never prune anything."""
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

    def test_a_failing_per_run_lookup_does_not_resolve_the_doubt(self):
        """"Could not ask" is not "the run ended".

        Asserts complete is True as well as the scoping: without that, a version
        of this test whose double fails EVERY query passes on the sweep query's
        complete=False and never exercises _elasticsearch_knows_run() at all.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-gone'}
        redis_client.get.return_value = None

        result = _manager(
            redis_client, _es(fail_per_run=True)
        ).get_active_run_workspaces()

        assert result.complete is True, "the sweep query succeeded; only the per-run one failed"
        assert result.unresolved_projects == {'codetoreum'}
        assert result.answers_for('codetoreum') is False

    def test_a_failing_sweep_query_marks_the_whole_answer_incomplete(self):
        """The other scope, kept distinct from the one above."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = Mock()
        es.search.side_effect = RuntimeError("elasticsearch down")

        result = _manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is False

    def test_a_run_redis_lost_but_elasticsearch_still_calls_active_is_protected(self):
        """#233's own shape: the long run whose record aged out while ES still
        holds it active. It must be protected by name AND not left in doubt."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:SDLC Execution:1045': 'run-live'}
        redis_client.get.return_value = None
        live = {
            'id': 'run-live', 'project': 'codetoreum', 'epic_id': '1016',
            'status': 'active', 'ended_at': None,
            'project_dir': '/workspace/.orchestrator/worktrees/codetoreum/1016',
        }

        result = _manager(
            redis_client, _es(active_hits=[live])
        ).get_active_run_workspaces()

        assert result.unresolved_projects == set()
        assert result.answers_for('codetoreum') is True
        assert result.protects('codetoreum', '/workspace/.orchestrator/worktrees/codetoreum/1016')

    def test_doubt_in_two_projects_marks_both(self):
        """The premise is many dangling pointers across projects, not one."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {
            'context-studio:1140': 'gone-a', 'test-project:dev:500': 'gone-b',
        }
        redis_client.get.return_value = None

        result = _manager(redis_client, _es()).get_active_run_workspaces()

        assert result.unresolved_projects == {'context-studio', 'test-project'}

    def test_a_capped_active_run_page_marks_the_answer_incomplete(self):
        """At the cap the result is a page, so "absent from it means ended" stops
        holding and a run beyond the window would be silently unprotected."""
        from services.pipeline_run import _ACTIVE_RUN_ES_PAGE
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        full_page = [
            {'id': f'r{i}', 'project': 'p', 'epic_id': str(i), 'ended_at': None}
            for i in range(_ACTIVE_RUN_ES_PAGE)
        ]

        result = _manager(
            redis_client, _es(active_hits=full_page)
        ).get_active_run_workspaces()

        assert result.complete is False

    def test_a_key_naming_no_project_falls_back_to_the_broad_gate(self):
        """The one branch where doubt cannot be scoped must not be the branch
        that drops it."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {':dev:500': 'run-gone'}
        redis_client.get.return_value = None

        result = _manager(redis_client, _es()).get_active_run_workspaces()

        assert result.complete is False
        assert result.answers_for('anything') is False


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


class TestTheMappingRubbleIsCollectedSafely:
    """Without collection the scoping above would suppress pruning indefinitely.
    But collection deletes the very pointer that raises the unknown, so the rule
    is: positive evidence, or age -- never "Elasticsearch did not mention it"."""

    MAP = 'orchestrator:pipeline_run:issue_mapping'
    STAMPS = 'orchestrator:pipeline_run:unknown_since'

    def test_a_run_elasticsearch_says_ended_is_removed_at_once(self):
        """Positive evidence. No need to wait this one out."""
        r = FakeRedis(mapping={'test-project:dev:500': 'run-done'})
        es = _es(by_id={'run-done': {'id': 'run-done', 'status': 'completed'}})

        assert _manager(r, es).cleanup_expired_mappings() == 1
        assert r.deleted == ['test-project:dev:500']

    def test_a_run_elasticsearch_says_ended_via_ended_at_is_removed(self):
        """Pins the `and not ended_at` conjunct: status may lag, ended_at does not."""
        r = FakeRedis(mapping={'test-project:dev:500': 'run-x'})
        es = _es(by_id={'run-x': {'id': 'run-x', 'status': 'active',
                                  'ended_at': '2026-09-13T14:00:00Z'}})

        assert _manager(r, es).cleanup_expired_mappings() == 1

    def test_a_run_elasticsearch_still_calls_active_is_kept(self):
        """The pointer #233 exists to preserve: record outlived Redis, ES says live."""
        r = FakeRedis(mapping={'codetoreum:SDLC Execution:1045': 'run-live'})
        es = _es(by_id={'run-live': {'id': 'run-live', 'status': 'active', 'ended_at': None}})

        assert _manager(r, es).cleanup_expired_mappings() == 0
        assert r.deleted == []

    def test_a_feedback_listening_run_is_kept_too(self):
        """Pins the second active status, not just 'active'."""
        r = FakeRedis(mapping={'codetoreum:SDLC Execution:1045': 'run-fb'})
        es = _es(by_id={'run-fb': {'id': 'run-fb', 'status': 'feedback_listening',
                                   'ended_at': None}})

        assert _manager(r, es).cleanup_expired_mappings() == 0

    def test_an_entry_neither_store_knows_is_NOT_deleted_on_first_sight(self):
        """The heart of it. Deleting here erases the unknown that makes the sweep
        skip the project -- on a 30-minute timer, converting a loud "I cannot
        tell" back into the silence #233 is about."""
        r = FakeRedis(mapping={'context-studio:1140': 'run-gone'})

        assert _manager(r, _es()).cleanup_expired_mappings() == 0
        assert r.deleted == []
        assert r.hget(self.STAMPS, 'context-studio:1140') is not None

    def test_it_is_collected_once_it_has_been_unknowable_longer_than_a_record_lives(self):
        """Real debris is by definition old, so the age gate still collects it."""
        import time as _t
        r = FakeRedis(mapping={'context-studio:1140': 'run-gone'})
        r.hset(self.STAMPS, 'context-studio:1140',
               f"{_t.time() - ACTIVE_RUN_REDIS_TTL_SECONDS - 60}:run-gone")

        assert _manager(r, _es()).cleanup_expired_mappings() == 1
        assert r.deleted == ['context-studio:1140']

    def test_a_stamp_belonging_to_a_different_run_restarts_the_clock(self):
        """Otherwise a key that went unknown, recovered, and went unknown again
        would inherit the first episode's age and be deleted early."""
        import time as _t
        r = FakeRedis(mapping={'context-studio:1140': 'run-new'})
        r.hset(self.STAMPS, 'context-studio:1140',
               f"{_t.time() - ACTIVE_RUN_REDIS_TTL_SECONDS - 60}:run-OLD")

        assert _manager(r, _es()).cleanup_expired_mappings() == 0

    def test_an_entry_with_a_live_redis_record_is_untouched(self):
        r = FakeRedis(mapping={'codetoreum:SDLC Execution:1045': 'run-1'},
                      records={'orchestrator:pipeline_run:run-1': _run_blob()})

        assert _manager(r, _es()).cleanup_expired_mappings() == 0

    def test_a_field_repointed_at_a_newer_run_is_not_wiped(self):
        """Between the read and the delete, get_or_create_pipeline_run() may have
        pointed this field at a live run. Compare-and-delete, not HDEL."""
        import time as _t
        r = FakeRedis(mapping={'context-studio:1140': 'run-gone'})
        r.hset(self.STAMPS, 'context-studio:1140',
               f"{_t.time() - ACTIVE_RUN_REDIS_TTL_SECONDS - 60}:run-gone")
        m = _manager(r, _es())
        r.hashes[self.MAP]['context-studio:1140'] = 'run-BRAND-NEW'   # concurrent writer

        assert m.cleanup_expired_mappings() == 0
        assert r.hashes[self.MAP]['context-studio:1140'] == 'run-BRAND-NEW'

    def test_a_field_repointed_mid_pass_is_not_wiped(self):
        """The real race, and the reason this uses compare-and-delete.

        This pass read 'run-gone' and decided to collect it; only THEN did
        get_or_create_pipeline_run() point the same field at a live run. An
        unconditional HDEL wipes the new run's mapping.

        The repoint must land AFTER hgetall. An earlier version of this test did
        it before, so the loop never saw the stale id, never reached the delete,
        and the whole compare-and-delete path went unexercised -- a mutation
        forcing the delete to always succeed passed the suite.
        """
        import time as _t
        key = 'context-studio:1140'
        r = FakeRedis(mapping={key: 'run-gone'})
        r.hset(self.STAMPS, key, f"{_t.time() - ACTIVE_RUN_REDIS_TTL_SECONDS - 60}:run-gone")
        r.on_exists = lambda fake: fake.hashes[self.MAP].__setitem__(key, 'run-BRAND-NEW')

        assert _manager(r, _es()).cleanup_expired_mappings() == 0, (
            "a field repointed at a live run must not be counted as collected"
        )
        assert r.hashes[self.MAP][key] == 'run-BRAND-NEW', (
            "the new run's mapping was wiped -- this needs compare-and-delete"
        )
        assert r.deleted == []

    def test_one_bad_entry_does_not_abandon_the_rest(self):
        """The whole-loop try this replaced reported a partial count as final."""
        r = FakeRedis(mapping={
            'p:dev:1': 'gone-1', 'p:dev:2': 'gone-2', 'p:dev:3': 'gone-3',
        })
        import time as _t
        for k, v in list(r.hashes[self.MAP].items()):
            r.hset(self.STAMPS, k, f"{_t.time() - ACTIVE_RUN_REDIS_TTL_SECONDS - 60}:{v}")
        r.fail_exists_for.add('orchestrator:pipeline_run:gone-2')

        assert _manager(r, _es()).cleanup_expired_mappings() == 2
        assert sorted(r.deleted) == ['p:dev:1', 'p:dev:3']

    def test_every_collectable_entry_is_collected_not_just_the_first(self):
        """The PR's premise is many dangling pointers, not one."""
        import time as _t
        r = FakeRedis(mapping={f'p:dev:{i}': f'gone-{i}' for i in range(3)})
        for k, v in list(r.hashes[self.MAP].items()):
            r.hset(self.STAMPS, k, f"{_t.time() - ACTIVE_RUN_REDIS_TTL_SECONDS - 60}:{v}")

        assert _manager(r, _es()).cleanup_expired_mappings() == 3

    def test_an_unreadable_elasticsearch_keeps_the_entry(self):
        """Fails CLOSED, opposite to the resolution path: this one deletes."""
        r = FakeRedis(mapping={'codetoreum:SDLC Execution:1045': 'run-?'})
        es = Mock()
        es.search.side_effect = RuntimeError("elasticsearch down")

        assert _manager(r, es).cleanup_expired_mappings() == 0
        assert r.deleted == []

    def test_no_elasticsearch_client_keeps_the_entry(self):
        r = FakeRedis(mapping={'codetoreum:SDLC Execution:1045': 'run-?'})
        m = _manager(r, _es())
        m.es = None

        assert m.cleanup_expired_mappings() == 0

    def test_a_failing_redis_does_not_raise(self):
        """Runs unattended on a scheduler."""
        redis_client = Mock()
        redis_client.hgetall.side_effect = RuntimeError("redis down")

        assert _manager(redis_client, _es()).cleanup_expired_mappings() == 0


class TestTheRecordOutlivesTheRun:
    """The root cause: an active run's Redis record expired while the run was
    still going, so the run read as "not active" to the Redis half of
    get_active_run_workspaces() and its epic worktree lost that protection.

    Sized against the RUN, not one agent: a run spans several agents plus review
    cycles and feedback waits. On the reference deployment 40 of 149 completed
    runs exceeded two hours and the longest took 51 hours."""

    def test_the_ttl_exceeds_the_longest_configured_agent_timeout(self):
        import yaml
        from pathlib import Path
        # THIS checkout's config, not /app's. /app is the production bind mount,
        # so reading it made the guard validate a different tree entirely: an
        # agent timeout raised on a branch would pass here while the constant it
        # is meant to constrain sat in the same branch, unchecked.
        cfg = Path(__file__).resolve().parents[3] / 'config/foundations/agents.yaml'
        agents = yaml.safe_load(cfg.read_text())['agents']
        longest = max(a.get('timeout', 0) for a in agents.values())

        assert longest > 0, "could not read agent timeouts"
        assert ACTIVE_RUN_REDIS_TTL_SECONDS > longest, (
            f"an agent may run {longest}s without writing, but its run record "
            f"expires after {ACTIVE_RUN_REDIS_TTL_SECONDS}s -- a live run would "
            f"go invisible to get_active_run_workspaces() (#233)"
        )

    def test_every_active_record_write_outlives_any_plausible_run(self):
        """A constant nothing uses proves nothing. The defect this replaces was
        three write sites that never referenced it -- including the status-update
        path, which reset an active run's record to one hour on every stage
        change, worse than the value the fix was raising."""
        import inspect, re
        from services import pipeline_run as pr

        src = inspect.getsource(pr)
        # setex(..., <literal>, ...) anywhere in the module's run-record writes.
        literals = [int(m) for m in re.findall(r'setex\(\s*[A-Za-z_.\[\]\'"]+,\s*(\d+),', src)]
        short = [t for t in literals if t < 3600]
        assert not short, f"a run record written with a sub-hour TTL: {short}"

        # Whatever is written for an ACTIVE run must clear the longest run we
        # have actually observed, with room to spare.
        assert pr.ACTIVE_RUN_REDIS_TTL_SECONDS >= 183969, (
            "the longest completed run measured on the reference deployment took "
            "183969s (51h); a record that expires before the run ends is the "
            "#233 root cause"
        )

    def test_the_status_update_path_uses_the_constant(self):
        """The specific site that made the first version of this fix ineffective."""
        import inspect
        from services.pipeline_run import PipelineRunManager
        src = inspect.getsource(PipelineRunManager.update_run_status)
        assert 'ACTIVE_RUN_REDIS_TTL_SECONDS' in src
        assert '3600' not in src, "an active run's record must not get a 1-hour TTL"

