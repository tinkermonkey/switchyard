"""
Shared environment variable builder for all Claude Code launches.

Both docker_runner.py (containerized) and claude_integration.py (local subprocess)
MUST use ClaudeEnvironmentBuilder to ensure OTEL telemetry env vars and auth vars
are injected consistently across every Claude Code instance.

Usage:
    from claude.environment import ClaudeEnvironmentBuilder, ClaudeRunContext

    ctx = ClaudeRunContext(
        agent_name=agent,
        task_id=task_id,
        project=project,
        issue_number=issue_number,
        pipeline_run_id=pipeline_run_id,
    )
    env_vars = ClaudeEnvironmentBuilder().build(ctx)

    # Containerized: extend docker run command
    for k, v in env_vars.items():
        cmd.extend(["-e", f"{k}={v}"])

    # Local subprocess: merge into inherited env
    env = {**os.environ.copy(), **env_vars}
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClaudeRunContext:
    """Identity and routing context for a single Claude Code invocation."""
    agent_name: str
    task_id: str
    project: str
    issue_number: Optional[str] = None
    pipeline_run_id: Optional[str] = None


class ClaudeEnvironmentBuilder:
    """
    Builds the complete set of environment variables for a Claude Code launch.

    Centralises auth, container identification, and OTEL telemetry configuration
    so that both the containerised (docker run) and local (subprocess.Popen) launch
    paths inject identical variables without duplication.

    Args:
        otel_host: Hostname of the OTEL collector. Defaults to the docker-compose
                   service name, which resolves correctly from both agent containers
                   (on switchyard_orchestrator-net) and the orchestrator container
                   (on orchestrator-net).
        otel_port: gRPC port of the OTEL collector (default 4317).
    """

    def __init__(
        self,
        otel_host: str = "otel-collector",
        otel_port: int = 4317,
    ):
        self._otel_host = otel_host
        self._otel_port = otel_port

    # ------------------------------------------------------------------
    # Sub-builders (exposed for testing and selective use)
    # ------------------------------------------------------------------

    def auth_vars(self) -> dict:
        """Claude Code authentication and third-party API keys."""
        vars: dict = {}

        oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        if oauth:
            vars["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
            logger.info("Using CLAUDE_CODE_OAUTH_TOKEN (subscription billing)")
        elif api_key:
            vars["ANTHROPIC_API_KEY"] = api_key
            logger.warning("Using ANTHROPIC_API_KEY (API billing)")
        else:
            logger.warning("No authentication token found — Claude Code will likely fail")

        if context7 := os.environ.get("CONTEXT7_API_KEY", "").strip():
            vars["CONTEXT7_API_KEY"] = context7

        return vars

    def identification_vars(self, ctx: ClaudeRunContext) -> dict:
        """Variables used by docker-claude-wrapper.py for Redis result persistence."""
        vars = {
            "REDIS_HOST": "redis",
            "REDIS_PORT": "6379",
            "AGENT": ctx.agent_name,
            "TASK_ID": ctx.task_id,
            "PROJECT": ctx.project,
        }
        if ctx.issue_number is not None:
            vars["ISSUE_NUMBER"] = str(ctx.issue_number)
        if ctx.pipeline_run_id:
            vars["PIPELINE_RUN_ID"] = ctx.pipeline_run_id
        return vars

    def otel_vars(self, ctx: ClaudeRunContext) -> dict:
        """
        OpenTelemetry environment variables for Claude Code telemetry.

        Enables Claude Code's built-in OTEL support to emit metrics and log events
        to the otel-collector service. Resource attributes attach agent/project/task
        identity to every span and metric so data can be correlated in Elasticsearch.

        Claude Code emits:
          Metrics: token.usage, cost.usage, lines_of_code.count, commit.count,
                   pull_request.count, active_time.total
          Logs:    tool_result, api_request, api_error, user_prompt (if enabled)
        """
        resource_attrs = [
            f"agent={ctx.agent_name}",
            f"project={ctx.project}",
            f"task_id={ctx.task_id}",
            "service.namespace=orchestrator",
        ]
        if ctx.issue_number:
            resource_attrs.append(f"issue_number={ctx.issue_number}")
        if ctx.pipeline_run_id:
            resource_attrs.append(f"pipeline_run_id={ctx.pipeline_run_id}")

        return {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{self._otel_host}:{self._otel_port}",
            # Log MCP server names and skill names for full tool attribution
            "OTEL_LOG_TOOL_DETAILS": "1",
            # 10s metric interval balances latency against Elasticsearch write volume
            "OTEL_METRIC_EXPORT_INTERVAL": "10000",
            # 5s log interval keeps tool call events timely for live dashboards
            "OTEL_LOGS_EXPORT_INTERVAL": "5000",
            "OTEL_RESOURCE_ATTRIBUTES": ",".join(resource_attrs),
        }

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    def build(self, ctx: ClaudeRunContext) -> dict:
        """
        Return the complete env var dict for a Claude Code launch.

        Combines auth, identification, OTEL telemetry, and GitHub vars.
        The caller merges this into a docker run command or subprocess env.
        """
        return {
            **self.auth_vars(),
            **self.identification_vars(ctx),
            **self.otel_vars(ctx),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            "GH_AUTH_SETUP_REQUIRED": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
