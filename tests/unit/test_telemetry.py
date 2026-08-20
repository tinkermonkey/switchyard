"""
Unit tests for monitoring/telemetry.py (OTLP metrics export for SigNoz).

These tests don't require the opentelemetry packages to actually be installed
or a real collector to be reachable — every branch is exercised either via the
disabled-by-default early returns (which run before any opentelemetry import)
or by monkeypatching the module's globals directly. The one exception is the
ImportError fallback test, which deterministically forces the import to fail
regardless of whether opentelemetry happens to be installed in the test
environment, so it's meaningful either way.

Module-level state note: _configured and _token_usage_counter are process-wide
singletons (see monitoring/telemetry.py's own docstring). Every test resets
both via the autouse fixture below so state never leaks between cases.
"""

import builtins
import logging

import pytest

from monitoring import telemetry


@pytest.fixture(autouse=True)
def reset_telemetry_module_state(monkeypatch):
    """Reset telemetry.py's module-level singletons before and after each test."""
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_token_usage_counter", None)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    yield


class TestSetupTelemetryNoOpGuards:
    """setup_telemetry() must stay a true no-op — no SDK import, no side effects —
    whenever it's not explicitly configured. This is the path every deployment
    without SigNoz hits on every single startup, so a regression here (e.g. an
    inverted condition) would start a real background export thread everywhere,
    including CI."""

    def test_noop_when_endpoint_unset(self, monkeypatch):
        # OTEL_EXPORTER_OTLP_ENDPOINT already absent via the autouse fixture
        assert telemetry.setup_telemetry() is False
        assert telemetry._configured is False
        assert telemetry._token_usage_counter is None

    def test_noop_when_endpoint_blank(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
        assert telemetry.setup_telemetry() is False
        assert telemetry._configured is False

    def test_noop_when_sdk_disabled_even_with_endpoint_set(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://example-collector:4318")
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        assert telemetry.setup_telemetry() is False
        assert telemetry._configured is False

    def test_sdk_disabled_check_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://example-collector:4318")
        monkeypatch.setenv("OTEL_SDK_DISABLED", "TRUE")
        assert telemetry.setup_telemetry() is False


class TestSetupTelemetryIdempotency:
    def test_second_call_is_noop_and_returns_true(self, monkeypatch):
        # Simulate telemetry already having been configured by an earlier call,
        # without going through the real OTEL SDK.
        monkeypatch.setattr(telemetry, "_configured", True)
        assert telemetry.setup_telemetry() is True
        # Endpoint was never set, so if this weren't a true no-op it would fall
        # through to the disabled-guard and return False instead.


class TestSetupTelemetryImportFailure:
    def test_missing_opentelemetry_package_disables_telemetry(self, monkeypatch, caplog):
        """Confirmed reachable in this dev environment: opentelemetry is not
        installed here even though requirements.txt lists it, so this isn't a
        hypothetical path. Forces the import to fail deterministically so the
        test is meaningful regardless of what's actually installed."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://example-collector:4318")

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ImportError(f"simulated missing package: {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with caplog.at_level(logging.WARNING):
            result = telemetry.setup_telemetry()

        assert result is False
        assert telemetry._configured is False
        assert telemetry._token_usage_counter is None
        assert any("opentelemetry packages not installed" in r.message for r in caplog.records)


class TestRecordClaudeTokenUsage:
    """record_claude_token_usage()'s behavior is what keeps ObservabilityManager.emit()
    safe by default — these tests use a fake counter double rather than the real
    OTEL SDK, since the logic under test (the None-guard, zero-value skip,
    model=None fallback) doesn't depend on the SDK at all."""

    class _FakeCounter:
        def __init__(self):
            self.calls = []

        def add(self, value, attributes=None):
            self.calls.append((value, dict(attributes or {})))

    def test_noop_when_not_configured(self):
        # _token_usage_counter is None via the autouse fixture — this must not
        # raise even though setup_telemetry() was never called.
        telemetry.record_claude_token_usage(
            project="p", agent="a", model="m", input_tokens=10, output_tokens=5
        )

    def test_records_one_point_per_nonzero_token_type(self, monkeypatch):
        counter = self._FakeCounter()
        monkeypatch.setattr(telemetry, "_token_usage_counter", counter)

        telemetry.record_claude_token_usage(
            project="proj-a", agent="senior_software_engineer", model="claude-sonnet-5",
            input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_creation_tokens=25,
        )

        # cache_read_tokens=0 must be skipped entirely - no empty data point
        recorded_types = {attrs["type"] for _, attrs in counter.calls}
        assert recorded_types == {"input", "output", "cacheCreation"}
        assert len(counter.calls) == 3

        by_type = {attrs["type"]: (value, attrs) for value, attrs in counter.calls}
        assert by_type["input"][0] == 100
        assert by_type["output"][0] == 50
        assert by_type["cacheCreation"][0] == 25

    def test_all_zero_tokens_records_nothing(self, monkeypatch):
        counter = self._FakeCounter()
        monkeypatch.setattr(telemetry, "_token_usage_counter", counter)

        telemetry.record_claude_token_usage(project="p", agent="a", model="m")

        assert counter.calls == []

    def test_common_attributes_tagged_on_every_point(self, monkeypatch):
        counter = self._FakeCounter()
        monkeypatch.setattr(telemetry, "_token_usage_counter", counter)

        telemetry.record_claude_token_usage(
            project="documentation_robotics", agent="pr_code_reviewer",
            model="claude-haiku-4-5", input_tokens=1,
        )

        _, attrs = counter.calls[0]
        assert attrs["project"] == "documentation_robotics"
        assert attrs["agent"] == "pr_code_reviewer"
        assert attrs["model"] == "claude-haiku-4-5"
        assert attrs["type"] == "input"

    def test_missing_model_falls_back_to_unknown_string(self, monkeypatch):
        """OTLP attribute values must be a concrete type - a raw None would be
        rejected by the SDK, so this must never reach the counter as None."""
        counter = self._FakeCounter()
        monkeypatch.setattr(telemetry, "_token_usage_counter", counter)

        telemetry.record_claude_token_usage(
            project="p", agent="a", model=None, input_tokens=1,
        )

        _, attrs = counter.calls[0]
        assert attrs["model"] == "unknown"
        assert attrs["model"] is not None


class TestShutdownTelemetry:
    """Flushing on shutdown is what prevents up to a minute of buffered metrics
    from being silently dropped on every orchestrator restart (see main.py's
    SIGTERM handler)."""

    def test_noop_when_never_configured(self, monkeypatch):
        # _configured is False via the autouse fixture. This must return
        # immediately without even attempting to import opentelemetry - if it
        # tried, and the package weren't installed, this would raise and the
        # bare `except Exception` inside shutdown_telemetry would swallow it,
        # masking the fact that the early-return branch was never reached.
        real_import = builtins.__import__
        imported_otel = []

        def spy_import(name, *args, **kwargs):
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                imported_otel.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", spy_import)

        telemetry.shutdown_telemetry()

        assert imported_otel == []

    @staticmethod
    def _inject_fake_opentelemetry_metrics(monkeypatch, metrics_module):
        """Make `from opentelemetry import metrics` resolve to a fake module,
        regardless of whether the real opentelemetry package is installed in
        this environment. `from X import Y` needs X itself importable (not
        just X.Y present in sys.modules), so a bare top-level package stub
        must exist too when the real one isn't installed."""
        import sys
        import types

        if "opentelemetry" in sys.modules:
            monkeypatch.setattr(sys.modules["opentelemetry"], "metrics", metrics_module)
        else:
            fake_pkg = types.ModuleType("opentelemetry")
            fake_pkg.metrics = metrics_module
            monkeypatch.setitem(sys.modules, "opentelemetry", fake_pkg)
        monkeypatch.setitem(sys.modules, "opentelemetry.metrics", metrics_module)

    def test_flushes_and_shuts_down_when_configured(self, monkeypatch):
        monkeypatch.setattr(telemetry, "_configured", True)

        calls = []

        class _FakeProvider:
            def force_flush(self, timeout_millis=None):
                calls.append(("force_flush", timeout_millis))

            def shutdown(self, timeout_millis=None):
                calls.append(("shutdown", timeout_millis))

        class _FakeMetricsModule:
            @staticmethod
            def get_meter_provider():
                return _FakeProvider()

        self._inject_fake_opentelemetry_metrics(monkeypatch, _FakeMetricsModule())

        telemetry.shutdown_telemetry(timeout_millis=1234)

        assert ("force_flush", 1234) in calls
        assert ("shutdown", 1234) in calls

    def test_flush_failure_is_caught_and_logged_not_raised(self, monkeypatch, caplog):
        monkeypatch.setattr(telemetry, "_configured", True)

        class _BrokenProvider:
            def force_flush(self, timeout_millis=None):
                raise RuntimeError("collector unreachable")

        class _FakeMetricsModule:
            @staticmethod
            def get_meter_provider():
                return _BrokenProvider()

        self._inject_fake_opentelemetry_metrics(monkeypatch, _FakeMetricsModule())

        with caplog.at_level(logging.WARNING):
            telemetry.shutdown_telemetry()  # must not raise

        assert any("Failed to flush/shutdown" in r.message for r in caplog.records)
