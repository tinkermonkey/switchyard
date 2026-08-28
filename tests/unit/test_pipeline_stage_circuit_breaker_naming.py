"""
Unit tests for PipelineStage's project-scoped CircuitBreaker construction
(switchyard issue #42).

pipeline/base.py's PipelineStage.__init__ constructs a fallback CircuitBreaker
(when the caller doesn't supply one explicitly) whenever the caller doesn't
pass its own circuit_breaker instance. That fallback must key the breaker as
"{project_name}:{name}" when project_name is available, and fall back to the
bare stage name (with a warning) for the handful of call sites that have no
project_name in scope, so as not to break them.
"""

import logging
from unittest.mock import Mock, patch

from pipeline.base import PipelineStage


class ConcreteStage(PipelineStage):
    """Minimal concrete PipelineStage for testing construction behavior."""

    async def execute(self, context):
        return context


class TestPipelineStageCircuitBreakerNaming:
    def test_project_name_is_prefixed_onto_breaker_name(self):
        with patch("pipeline.base.CircuitBreaker") as mock_cb_cls:
            mock_cb_cls.return_value = Mock()
            ConcreteStage(name="developer", project_name="context-studio")

        mock_cb_cls.assert_called_once_with(name="context-studio:developer")

    def test_explicit_circuit_breaker_is_not_overridden(self):
        """When a caller passes its own CircuitBreaker (e.g. AgentStage's
        custom circuit_breaker_config path), PipelineStage must use it as-is
        rather than constructing a second one."""
        explicit_breaker = Mock()
        with patch("pipeline.base.CircuitBreaker") as mock_cb_cls:
            stage = ConcreteStage(
                name="developer",
                circuit_breaker=explicit_breaker,
                project_name="context-studio",
            )

        mock_cb_cls.assert_not_called()
        assert stage.circuit_breaker is explicit_breaker

    def test_missing_project_name_falls_back_to_bare_name_with_warning(self, caplog):
        """Callers with no project_name in scope (e.g. create_agent_pipeline)
        must still construct successfully rather than break, reproducing
        pre-fix (un-namespaced) behavior for just that call site."""
        with patch("pipeline.base.CircuitBreaker") as mock_cb_cls:
            mock_cb_cls.return_value = Mock()
            with caplog.at_level(logging.WARNING):
                ConcreteStage(name="developer")

        mock_cb_cls.assert_called_once_with(name="developer")
        assert any(
            "without project_name" in record.message for record in caplog.records
        )

    def test_project_name_stored_on_stage(self):
        with patch("pipeline.base.CircuitBreaker"):
            stage = ConcreteStage(name="developer", project_name="context-studio")

        assert stage.project_name == "context-studio"
