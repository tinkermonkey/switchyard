"""
Unit tests for AgentStage's project-scoped custom CircuitBreaker construction
(switchyard issue #42).

agents/orchestrator_integration.py's AgentStage.__init__ has its own
CircuitBreaker construction path for agents configured with a custom
circuit_breaker_config (bypassing PipelineStage's fallback). That path must
also key the breaker as "{project_name}:{agent_name}".

Requires the Docker container environment (agents/__init__.py transitively
imports services.dev_container_state, which creates state directories under
/app at import time) — skipped outside it, matching the existing pattern in
tests/unit/test_orchestrator_integration_ux.py.
"""

import os
import pytest
from unittest.mock import Mock, patch

if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)


def _agent_config_with_circuit_breaker(**cb_overrides):
    real_config = Mock()
    real_config.circuit_breaker_config = {
        'failure_threshold': 3,
        'recovery_timeout': 30,
        'success_threshold': 2,
        **cb_overrides,
    }
    return {'agent_config': real_config}


class TestAgentStageCircuitBreakerNaming:
    def test_custom_circuit_breaker_config_uses_project_prefix(self):
        from agents.orchestrator_integration import AgentStage

        agent_config = _agent_config_with_circuit_breaker()

        with patch('agents.orchestrator_integration.CircuitBreaker') as mock_cb_cls, \
             patch('agents.orchestrator_integration.get_agent_class') as mock_get_agent_class:
            mock_cb_cls.return_value = Mock()
            mock_agent_class = Mock()
            mock_get_agent_class.return_value = mock_agent_class

            AgentStage("developer", agent_config, project_name="context-studio")

        mock_cb_cls.assert_called_once_with(
            name="context-studio:developer",
            failure_threshold=3,
            recovery_timeout=30,
            success_threshold=2,
        )

    def test_custom_circuit_breaker_config_without_project_name_falls_back(self):
        """No project_name in scope (e.g. create_agent_pipeline) must still
        construct successfully, reproducing pre-fix un-namespaced behavior
        for just that call site rather than breaking it."""
        from agents.orchestrator_integration import AgentStage

        agent_config = _agent_config_with_circuit_breaker()

        with patch('agents.orchestrator_integration.CircuitBreaker') as mock_cb_cls, \
             patch('agents.orchestrator_integration.get_agent_class') as mock_get_agent_class:
            mock_cb_cls.return_value = Mock()
            mock_get_agent_class.return_value = Mock()

            AgentStage("developer", agent_config)

        mock_cb_cls.assert_called_once_with(
            name="developer",
            failure_threshold=3,
            recovery_timeout=30,
            success_threshold=2,
        )

    def test_two_projects_same_agent_get_distinctly_named_breakers(self):
        """Two AgentStage instances for the same agent in different projects
        must not resolve to the same CircuitBreaker name."""
        from agents.orchestrator_integration import AgentStage

        seen_names = []

        def record_name(*args, **kwargs):
            seen_names.append(kwargs.get('name'))
            return Mock()

        with patch('agents.orchestrator_integration.CircuitBreaker', side_effect=record_name), \
             patch('agents.orchestrator_integration.get_agent_class') as mock_get_agent_class:
            mock_get_agent_class.return_value = Mock()

            AgentStage("developer", _agent_config_with_circuit_breaker(), project_name="proj_a")
            AgentStage("developer", _agent_config_with_circuit_breaker(), project_name="proj_b")

        assert seen_names == ["proj_a:developer", "proj_b:developer"]
        assert len(set(seen_names)) == 2
