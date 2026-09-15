"""
#233: an ACTIVE pipeline run's Redis record expired while the run was still
going, so the run read as "not active" to get_active_run_workspaces() -- the
check that stops the startup sweep deleting its epic worktree.

Two things made that routine rather than rare. The TTL was sized against an
agent timeout when a run is not an agent (it spans several, plus review cycles
and feedback waits), and four of the five write sites did not use the sized
value at all -- one of them update_run_status(), which runs on every status
change and so reset the record to one hour immediately after creation wrote
something longer.

Measured on the reference deployment, 2026-09-15, over the 160 runs that had
ended (153 completed, 7 failed): median 34m, p90 2.8h, max 51.1h; 51 past an
hour, 41 past two, and none past seven days.

The structural test below is the one that matters. An earlier version of this
guard asserted only that no TTL was below 3600, which every value that
reintroduces the bug clears -- reverting creation to its original 7200 passed
the whole suite.
"""

import ast
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.pipeline_run import ACTIVE_RUN_REDIS_TTL_SECONDS, PipelineRunManager

#: Longest run actually observed, in seconds (51.1h). The TTL has to clear this
#: with room, or the record expires under a run that is still going.
LONGEST_OBSERVED_RUN_SECONDS = 183969

#: The only methods allowed to write a run record with a literal TTL. Each ends
#: a run, so its record is a corpse kept briefly for late readers -- a short
#: life is correct there and must not be confused with the active-run TTL.
_TERMINAL_WRITERS = frozenset({
    'end_pipeline_run',
    'end_phantom_pipeline_run',
    '_end_run_in_elasticsearch',
})

_SOURCE = Path(PipelineRunManager.__module__.replace('.', '/') + '.py')


def _setex_calls():
    """Every redis.setex in services/pipeline_run.py, with its enclosing function.

    AST rather than a regex: the regex this replaces matched on the shape of the
    first argument, so rewriting the call as `setex(self._get_redis_key(x), 60,
    ...)` -- a sixty-second TTL on the primary run record -- slipped past it.
    """
    src = Path('/app/services/pipeline_run.py')
    src = src if src.exists() else _SOURCE
    tree = ast.parse(Path('services/pipeline_run.py').read_text())

    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if hasattr(child, 'lineno'):
                    owner.setdefault(child.lineno, node.name)

    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'setex'
                and len(node.args) > 1):
            yield owner.get(node.lineno, '<module>'), node.lineno, node.args[1]


class TestEveryActiveRunWriteUsesTheSizedTTL:
    """The defect was four write sites that ignored the constant, so a test over
    the constant alone could not see it."""

    def test_no_active_run_write_uses_a_literal_ttl(self):
        offenders = [
            (fn, line, getattr(ttl, 'value', ast.dump(ttl)))
            for fn, line, ttl in _setex_calls()
            if fn not in _TERMINAL_WRITERS
            and not (isinstance(ttl, ast.Name) and ttl.id == 'ACTIVE_RUN_REDIS_TTL_SECONDS')
        ]
        assert not offenders, (
            "these write a run record with a TTL that is not "
            f"ACTIVE_RUN_REDIS_TTL_SECONDS: {offenders}. A run that outlives its "
            "own record reads as 'not active' and loses its worktree (#233)."
        )

    def test_the_terminal_writers_are_still_the_only_exemptions(self):
        """Control, and a tripwire: if a write moves into one of these methods
        the exemption must be re-argued, not inherited."""
        exempt = {fn for fn, _, _ in _setex_calls() if fn in _TERMINAL_WRITERS}
        assert exempt == _TERMINAL_WRITERS, (
            f"the exemption list no longer matches the code: {exempt}"
        )

    def test_there_are_still_writes_to_check(self):
        """Stops the whitelist test passing vacuously if setex is refactored away."""
        active = [fn for fn, _, _ in _setex_calls() if fn not in _TERMINAL_WRITERS]
        assert len(active) >= 5, f"expected the five active-run writes, found {active}"


class TestTheTTLOutlivesRealRuns:
    def test_it_clears_the_longest_run_observed(self):
        assert ACTIVE_RUN_REDIS_TTL_SECONDS > LONGEST_OBSERVED_RUN_SECONDS, (
            f"the longest run measured took {LONGEST_OBSERVED_RUN_SECONDS}s "
            f"(51.1h); a record expiring at {ACTIVE_RUN_REDIS_TTL_SECONDS}s "
            "would vanish while that run was still going (#233)"
        )

    def test_it_clears_the_longest_agent_timeout_too(self):
        """Necessary but not sufficient -- a run is several agents. Kept so that
        raising an agent timeout fails here rather than silently."""
        import yaml
        cfg = Path(__file__).resolve().parents[3] / 'config/foundations/agents.yaml'
        agents = yaml.safe_load(cfg.read_text())['agents']
        longest = max(a['timeout'] for a in agents.values() if 'timeout' in a)

        assert longest > 0
        assert ACTIVE_RUN_REDIS_TTL_SECONDS > longest


class TestTheWritesActuallyPassIt:
    """Behavioural backstop: the structural test reads the source, these drive
    the code. The fake records the TTL, which the previous fake discarded."""

    class RecordingRedis:
        def __init__(self, records=None):
            self.ttls = {}
            self.records = dict(records or {})
            self.hashes = {}

        def setex(self, key, ttl, value):
            self.ttls[key] = ttl
            self.records[key] = value

        def get(self, key):
            return self.records.get(key)

        def exists(self, key):
            return 1 if key in self.records else 0

        def hset(self, name, field, value):
            self.hashes.setdefault(name, {})[field] = value
            return 1

        def hget(self, name, field):
            return self.hashes.get(name, {}).get(field)

        def hgetall(self, name):
            return dict(self.hashes.get(name, {}))

        def hdel(self, name, field):
            return 1 if self.hashes.get(name, {}).pop(field, None) is not None else 0

    def _manager(self, redis_client):
        return PipelineRunManager(
            redis_client=redis_client, elasticsearch_client=Mock(), manage_schema=False
        )

    def test_creating_a_run_writes_the_sized_ttl(self):
        r = self.RecordingRedis()
        self._manager(r).create_pipeline_run(
            issue_number=1045, issue_title='Phase 1',
            issue_url='https://github.com/o/r/issues/1045',
            project='codetoreum', board='SDLC Execution',
        )
        assert set(r.ttls.values()) == {ACTIVE_RUN_REDIS_TTL_SECONDS}

    def test_moving_a_run_back_to_active_does_not_shorten_it(self):
        """The site that undid the others: it ran on every status change, so a
        run created with a long TTL was cut back to an hour moments later."""
        r = self.RecordingRedis()
        m = self._manager(r)
        run = m.create_pipeline_run(
            issue_number=1045, issue_title='Phase 1',
            issue_url='https://github.com/o/r/issues/1045',
            project='codetoreum', board='SDLC Execution',
        )
        r.ttls.clear()

        m.update_run_status('codetoreum', 1045, 'active', board='SDLC Execution')

        assert r.ttls, "update_run_status wrote no record"
        assert set(r.ttls.values()) == {ACTIVE_RUN_REDIS_TTL_SECONDS}, (
            f"an active run's record was rewritten with {set(r.ttls.values())}"
        )

    def test_a_field_update_does_not_shorten_it_either(self):
        r = self.RecordingRedis()
        m = self._manager(r)
        run = m.create_pipeline_run(
            issue_number=1045, issue_title='Phase 1',
            issue_url='https://github.com/o/r/issues/1045',
            project='codetoreum', board='SDLC Execution',
        )
        r.ttls.clear()

        m._persist_run_update(run)

        assert set(r.ttls.values()) == {ACTIVE_RUN_REDIS_TTL_SECONDS}
