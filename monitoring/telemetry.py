"""
OpenTelemetry metrics export for the Switchyard orchestrator.

Call setup_telemetry() once at startup before recording any metrics. If
OTEL_EXPORTER_OTLP_ENDPOINT is not set or OTEL_SDK_DISABLED=true, this is a
no-op and record_claude_token_usage() silently does nothing.

This exports orchestrator-side metrics derived from the existing
ObservabilityManager event stream (CLAUDE_API_CALL_COMPLETED / _FAILED). It is
distinct from the OTEL_COLLECTOR_HOST pipeline in claude/environment.py, which
carries Claude Code's own self-reported CLI telemetry from agent containers to
the local otel-collector -> Elasticsearch pipeline. This module instead points
at the external OTLP collector (e.g. the one phone-home uses for SigNoz) via
the standard OTEL_EXPORTER_OTLP_ENDPOINT env var, so the two never collide.

Environment variables (standard OTEL):
  OTEL_EXPORTER_OTLP_ENDPOINT  — OTLP HTTP base URL (e.g. http://otel-collector:4318)
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
