"""
Unit test for DockerAgentRunner._get_agent_container_limiter()'s caching.

The tier-0 concurrency limiter (services/agent_container_concurrency.py)
must be constructed once per DockerAgentRunner instance and reused across
every run_agent_in_container() call — not just to reuse the Redis
connection, but so its ORCHESTRATOR_WORKERS-vs-cap mismatch warning logs
once at first use instead of on every single agent launch.
"""

from unittest.mock import MagicMock, patch


def test_agent_container_limiter_is_constructed_once_and_reused():
    with patch("claude.docker_runner.DockerAgentRunner._get_redis", return_value=None):
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner()
        assert runner._agent_container_limiter is None

        first = runner._get_agent_container_limiter()
        second = runner._get_agent_container_limiter()

        assert first is second
        assert runner._agent_container_limiter is first
