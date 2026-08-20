"""
OpenTelemetry metrics export for the Switchyard orchestrator.

Call setup_telemetry() once at startup before recording any metrics, and
shutdown_telemetry() once at process shutdown to flush anything buffered. If
OTEL_EXPORTER_OTLP_ENDPOINT is not set or OTEL_SDK_DISABLED=true, both are
no-ops and record_claude_token_usage() silently does nothing.

This exports orchestrator-side metrics derived from the existing
ObservabilityManager event stream (CLAUDE_API_CALL_COMPLETED / _FAILED). It is
distinct from the OTEL_COLLECTOR_HOST-derived vars that claude/environment.py's
ClaudeEnvironmentBuilder injects into each Claude CLI launch, which carry
Claude Code's own self-reported CLI telemetry from agent containers to the
local otel-collector -> Elasticsearch pipeline. This module instead points at
an external OTLP collector (e.g. SigNoz) via the standard
OTEL_EXPORTER_OTLP_ENDPOINT env var, so the two never collide — see
.env.example for the distinction and a warning against pointing this at the
local otel-collector service, which has no SigNoz exporter configured.

Note on delivery failures: setup_telemetry() and record_claude_token_usage()
only guard against local/in-process errors (bad config, SDK import failure, a
broken counter). The actual network export to the collector runs on a
PeriodicExportingMetricReader background thread this module does not
supervise — a real delivery failure (unreachable collector, auth rejected,
TLS error) surfaces only via the OpenTelemetry SDK's own internal logging
(logger namespaces under `opentelemetry.*`), not as a switchyard log line.
Those loggers are not suppressed by services/logging_config.py, so their
WARNING/ERROR output does reach the same log stream as everything else, but
there is currently no switchyard-level health signal (e.g. on /health) for
export health specifically.

Environment variables (standard OTEL):
  OTEL_EXPORTER_OTLP_ENDPOINT  — OTLP HTTP base URL (e.g. http://your-signoz-collector:4318)
  OTEL_EXPORTER_OTLP_HEADERS   — Comma-separated auth headers (e.g. api-key=secret)
  OTEL_SERVICE_NAME             — Override the default service name passed to setup_telemetry()
  OTEL_RESOURCE_ATTRIBUTES      — Extra resource attrs (key=val,key=val)
  OTEL_SDK_DISABLED             — Set to "true" to disable all telemetry
"""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_configured = False
_token_usage_counter = None

DEFAULT_SERVICE_NAME = "switchyard-claude"

# Mirrors Claude Code's own claude_code.token.usage metric shape (value = token
# count, tag `type` = input/output/cacheRead/cacheCreation) but named distinctly
# so it is never confused with genuine CLI-side self-reported metrics.
TOKEN_USAGE_METRIC_NAME = "switchyard.claude.token.usage"

# Bounds how long shutdown_telemetry() can block on a slow/unreachable
# collector. Kept well under typical container stop grace periods (docker
# defaults to 10s) so a bad SigNoz endpoint can't hang orchestrator shutdown.
DEFAULT_SHUTDOWN_TIMEOUT_MILLIS = 5000


def setup_telemetry(default_service_name: str = DEFAULT_SERVICE_NAME) -> bool:
    """
    Initialize the OTEL MeterProvider and the switchyard.claude.token.usage counter.

    Returns True if telemetry was configured, False if disabled or endpoint missing.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _configured, _token_usage_counter
    if _configured:
        return True

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint or os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return False

    # Set service name before Resource.create() so it picks up our default.
    if not os.getenv("OTEL_SERVICE_NAME"):
        os.environ["OTEL_SERVICE_NAME"] = default_service_name

    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError as exc:
        log.warning("opentelemetry packages not installed — telemetry disabled: %s", exc)
        return False

    # Resource picks up OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES from env.
    resource = Resource.create()

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    meter = metrics.get_meter("switchyard.claude")
    _token_usage_counter = meter.create_counter(
        TOKEN_USAGE_METRIC_NAME,
        unit="{token}",
        description=(
            "Claude token usage recorded by the Switchyard orchestrator from its "
            "internal agent-events tracking, tagged by type/project/agent/model."
        ),
    )

    _configured = True
    log.info(
        "OTEL telemetry configured: service=%s endpoint=%s",
        os.getenv("OTEL_SERVICE_NAME"),
        endpoint,
    )
    return True


def record_claude_token_usage(project: str, agent: str, model: Optional[str],
                               input_tokens: int = 0, output_tokens: int = 0,
                               cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> None:
    """Record Claude token usage as OTLP counter data points, one per token type present."""
    if _token_usage_counter is None:
        return

    attrs_base = {"project": project, "agent": agent, "model": model or "unknown"}

    for token_type, value in (
        ("input", input_tokens),
        ("output", output_tokens),
        ("cacheRead", cache_read_tokens),
        ("cacheCreation", cache_creation_tokens),
    ):
        if value:
            _token_usage_counter.add(value, attributes={**attrs_base, "type": token_type})


def shutdown_telemetry(timeout_millis: int = DEFAULT_SHUTDOWN_TIMEOUT_MILLIS) -> None:
    """
    Flush any buffered token-usage metrics and shut down the MeterProvider.

    PeriodicExportingMetricReader batches on its own timer (default 60s), so
    without an explicit flush at shutdown, up to a minute of accumulated
    counter increments is silently dropped on every process stop/restart —
    no exception, just data that never gets exported. Call this once from the
    process's shutdown path (see main.py's SIGTERM handler).

    Safe to call even if setup_telemetry() was never called or no-op'd (e.g.
    endpoint never configured) — nothing to flush in that case, and this
    returns immediately without importing the opentelemetry packages.
    """
    if not _configured:
        return
    try:
        from opentelemetry import metrics
        provider = metrics.get_meter_provider()
        provider.force_flush(timeout_millis=timeout_millis)
        provider.shutdown(timeout_millis=timeout_millis)
        log.info("OTEL telemetry flushed and shut down")
    except Exception as e:
        log.warning(f"Failed to flush/shutdown OTEL telemetry cleanly: {e}")
