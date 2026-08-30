"""
Unit tests for AgentExecutor._queue_environment_verifier()'s previous_stage_output handoff.

Covers the fix that replaced a hardcoded 'Setup agent completed successfully' placeholder
with the dev_environment_setup agent's actual output, so the verifier's Step 0 ("don't
trust the setup agent's narrative summary") has a real narrative to scrutinize.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, patch

from services.agent_executor import AgentExecutor


@pytest.fixture
def agent_executor():
    """Create an AgentExecutor instance."""
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestQueueEnvironmentVerifierOutputHandoff:
    """Tests for previous_stage_output in the queued verifier task's context."""

    @pytest.mark.asyncio
    async def test_passes_through_actual_setup_output(self, agent_executor):
        """The verifier's previous_stage_output must be the setup agent's real output,
        not a generic placeholder — otherwise Step 0's narrative-distrust check has
        nothing real to scrutinize."""
        with patch('task_queue.task_manager.TaskQueue') as MockTaskQueue, \
             patch('services.agent_executor.config_manager') as mock_config:
            mock_config.get_project_config.side_effect = Exception("no project config in test")
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            setup_output = (
                "### Summary\nInvestigated black==26.5.1 — resolves fine now, no change needed."
            )

            await agent_executor._queue_environment_verifier(
                "test-project", {'board': 'system'}, setup_output=setup_output
            )

            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            assert enqueued_task.context['previous_stage_output'] == setup_output

    @pytest.mark.asyncio
    async def test_falls_back_to_placeholder_when_no_setup_output(self, agent_executor):
        """When no setup output could be extracted, previous_stage_output must still be
        a non-empty string — the verifier agent raises if it's falsy — but must not
        claim success (the setup agent's actual outcome is unknown)."""
        with patch('task_queue.task_manager.TaskQueue') as MockTaskQueue, \
             patch('services.agent_executor.config_manager') as mock_config:
            mock_config.get_project_config.side_effect = Exception("no project config in test")
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            await agent_executor._queue_environment_verifier(
                "test-project", {'board': 'system'}, setup_output=None
            )

            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            previous_stage_output = enqueued_task.context['previous_stage_output']
            assert previous_stage_output  # non-empty, so the verifier agent doesn't raise
            assert "successfully" not in previous_stage_output.lower()
