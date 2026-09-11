"""The ILM policies have to actually reach Elasticsearch, and reach the right indices.

`config/retention.py` making every policy body agree is only half of it. The
other half is the part that runs against a live cluster at startup, and it is
the half with no test before this file: three services did get-then-create, so
a policy could never change after the first boot; and the four token-metrics
families got `index.lifecycle.name` added to their index TEMPLATES, which apply
at index creation and therefore reach nothing that already exists.

Both of those fail the same way -- retention is reported as applied, and is not.
Neither shows up as an error.
"""

import os
from unittest.mock import MagicMock

import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from config.retention import (  # noqa: E402
    MONTHLY_INDEX_PERIOD_DAYS,
    RETENTION_DAYS,
    delete_phase_days,
)


def _delete_min_age(body) -> str:
    return body['policy']['phases']['delete']['min_age']


class TestPoliciesArePutUnconditionally:
    """get-then-create-if-missing makes a policy that can never be changed.

    ILM re-reads a policy rather than stamping it onto an index, so a changed
    RETENTION_DAYS reaches data that already exists -- but only if the put
    actually happens. Guarding it with `except NotFoundError` means the value
    on a cluster is whatever it was the first time the service ever booted.
    `put_lifecycle` is idempotent, so there is nothing to guard against.
    """

    def test_token_metrics_puts_its_policy_and_adopts_existing_indices(self):
        from services.token_metrics_service import (
            TOKEN_METRICS_ILM_POLICY,
            TokenMetricsService,
        )

        es = MagicMock()
        service = TokenMetricsService.__new__(TokenMetricsService)
        service.es = es
        service._ensure_index_templates()

        es.ilm.get_lifecycle.assert_not_called()
        es.ilm.put_lifecycle.assert_called_once()
        assert es.ilm.put_lifecycle.call_args.kwargs['name'] == TOKEN_METRICS_ILM_POLICY
        assert _delete_min_age(es.ilm.put_lifecycle.call_args.kwargs['body']) == \
            f'{delete_phase_days(MONTHLY_INDEX_PERIOD_DAYS)}d'

        # Every template must carry the policy...
        assert es.indices.put_index_template.call_count == 4
        for call in es.indices.put_index_template.call_args_list:
            settings = call.kwargs['body']['template']['settings']
            assert settings['index.lifecycle.name'] == TOKEN_METRICS_ILM_POLICY

        # ...and, because a template only applies at index creation, the
        # indices that already exist must be adopted explicitly. Without this
        # the 8,010 agent-execution-summaries documents that motivated the
        # change stay exactly as unmanaged as they were, while the log says the
        # policy was applied.
        adopted = {
            call.kwargs['index'] for call in es.indices.put_settings.call_args_list
        }
        assert adopted == {
            'token-metrics-agents-*',
            'token-metrics-agents-hourly-*',
            'token-metrics-cycles-hourly-*',
            'agent-execution-summaries-*',
        }
        for call in es.indices.put_settings.call_args_list:
            assert call.kwargs['body'] == {
                'index.lifecycle.name': TOKEN_METRICS_ILM_POLICY
            }

    def test_project_metrics_puts_its_policy_unconditionally(self):
        from services.pattern_detection_schema import PROJECT_METRICS_ILM_POLICY
        from services.project_metrics_service import ProjectMetricsService

        es = MagicMock()
        service = ProjectMetricsService.__new__(ProjectMetricsService)
        service.es = es
        service._ensure_index_template()

        es.ilm.get_lifecycle.assert_not_called()
        es.ilm.put_lifecycle.assert_called_once()
        body = es.ilm.put_lifecycle.call_args.kwargs['body']
        assert _delete_min_age(body) == _delete_min_age(PROJECT_METRICS_ILM_POLICY)

    def test_test_cycle_recorder_puts_its_policy_unconditionally(self):
        from monitoring.test_cycle_recorder import TestCycleRecorder
        from services.pattern_detection_schema import TEST_CYCLE_RECORDS_ILM_POLICY

        es = MagicMock()
        recorder = TestCycleRecorder.__new__(TestCycleRecorder)
        recorder.es = es
        recorder._ensure_indices()

        es.ilm.get_lifecycle.assert_not_called()
        es.ilm.put_lifecycle.assert_called_once()
        assert es.ilm.put_lifecycle.call_args.kwargs['body'] is \
            TEST_CYCLE_RECORDS_ILM_POLICY

    def test_metrics_collector_puts_the_rollover_policy(self):
        """The only family that rolls over on size as well as date, and the one
        whose body is built inline rather than held in a module constant --
        so nothing else in the suite covers it."""
        from monitoring.metrics import MetricsCollector

        es = MagicMock()
        collector = MetricsCollector.__new__(MetricsCollector)
        collector.es = es
        collector._create_index_templates()

        es.ilm.put_lifecycle.assert_called_once()
        body = es.ilm.put_lifecycle.call_args.kwargs['body']
        assert _delete_min_age(body) == f'{RETENTION_DAYS}d'
        assert 'rollover' in body['policy']['phases']['hot']['actions']


class TestTheWarmPhaseCannotStallShortOfDeleting:

    def test_warm_never_migrates(self):
        """ILM injects a `migrate` action into warm unless it is switched off,
        and migrate blocks until every shard copy is active. On a single node
        any index with replicas > 0 is permanently yellow, so it waits forever
        and never reaches delete -- which is how both claude-otel data streams
        sat in warm/migrate/check-migration for two months under a policy that
        claimed a 14-day window."""
        from config.retention import build_ilm_policy

        warm = build_ilm_policy()['policy']['phases'].get('warm')
        assert warm is not None, "this test is meaningless if warm was dropped"
        assert warm['actions']['migrate'] == {'enabled': False}


class TestDataStreamsCanReachTheirDeletePhase:

    def test_the_otel_policy_rolls_over(self):
        """ILM refuses to delete the write index of a data stream, and the OTEL
        streams have no other rollover trigger. Without a rollover action the
        single backing index stays the write index forever and the delete phase
        is simply unreachable -- the policy is present, correct, and inert."""
        from services.pattern_detection_schema import (
            CLAUDE_OTEL_ILM_POLICY,
            CLAUDE_OTEL_LOGS_TEMPLATE,
            CLAUDE_OTEL_METRICS_TEMPLATE,
        )

        for template in (CLAUDE_OTEL_LOGS_TEMPLATE, CLAUDE_OTEL_METRICS_TEMPLATE):
            assert 'data_stream' in template, \
                "this test only applies to data streams"

        hot = CLAUDE_OTEL_ILM_POLICY['policy']['phases']['hot']['actions']
        assert 'rollover' in hot, (
            "a data-stream policy with no rollover can never delete anything"
        )
