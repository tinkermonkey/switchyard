import subprocess
import json
import logging
import os
from typing import Dict, Any, Optional, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


class DockerAgentRunner:
    """Runs Claude Code agents in isolated Docker containers"""

    def __init__(self, network_name: str = "clauditoreum_orchestrator-net"):
        # Docker Compose prefixes network names with project directory name
        self.network_name = network_name

    async def run_agent_in_container(
        self,
        prompt: str,
        context: Dict[str, Any],
        project_dir: Path,
        mcp_servers: list = None,
        stream_callback: Optional[Callable] = None
    ) -> str:
        """
        Execute Claude Code agent in an isolated Docker container

        Args:
            prompt: The prompt to send to Claude
            context: Context including project, task, agent info
            project_dir: Path to the project directory to mount
            mcp_servers: MCP server configurations
            stream_callback: Optional callback for streaming output

        Returns:
            Agent output as string
        """
        agent = context.get('agent', 'unknown')
        task_id = context.get('task_id', 'unknown')
        project = context.get('project', 'unknown')

        logger.info(f"Running agent {agent} in Docker container for project {project}")

        # Prepare MCP configuration if needed
        mcp_config = None
        if mcp_servers and len(mcp_servers) > 0:
            mcp_config = self._prepare_mcp_config(mcp_servers, project_dir)

        # Build docker run command
        container_name = f"claude-agent-{project}-{task_id}"
        docker_cmd = self._build_docker_command(
            container_name=container_name,
            project_dir=project_dir,
            mcp_config=mcp_config,
            context=context
        )

        try:
            # Start the container and execute Claude Code
            result_text = await self._execute_in_container(
                docker_cmd=docker_cmd,
                prompt=prompt,
                container_name=container_name,
                stream_callback=stream_callback,
                context=context
            )

            return result_text

        finally:
            # Clean up container
            self._cleanup_container(container_name)

    def _prepare_mcp_config(self, mcp_servers: list, project_dir: Path) -> Dict:
        """Prepare MCP configuration for the container"""
        mcp_config_data = {"mcpServers": {}}

        for server in mcp_servers:
            server_name = server['name']
            server_type = server.get('type', 'http')

            if server_type == 'http':
                url = server['url']
                logger.info(f"Adding HTTP MCP server: {server_name} at {url}")
                mcp_config_data["mcpServers"][server_name] = {
                    "type": "http",
                    "url": url
                }
            elif server_type == 'stdio':
                command = server['command']
                args = server.get('args', [])
                server_env = server.get('env', {})

                # Substitute {work_dir} with /workspace (container path)
                substituted_args = []
                for arg in args:
                    if isinstance(arg, str) and '{work_dir}' in arg:
                        arg = arg.replace('{work_dir}', '/workspace')
                    substituted_args.append(arg)

                logger.info(f"Adding stdio MCP server: {server_name}")
                mcp_config_data["mcpServers"][server_name] = {
                    "command": command,
                    "args": substituted_args
                }
                if server_env:
                    mcp_config_data["mcpServers"][server_name]["env"] = server_env

        return mcp_config_data

    def _build_docker_command(
        self,
        container_name: str,
        project_dir: Path,
        mcp_config: Optional[Dict],
        context: Dict[str, Any]
    ) -> list:
        """Build the docker run command"""

        agent = context.get('agent', 'unknown')

        # Convert container project path to host path for Docker-in-Docker
        # In docker-compose.yml: - ..:/workspace means /workspace maps to host's ~/workspace/orchestrator
        host_workspace = os.environ.get('HOST_WORKSPACE_PATH', '/workspace')
        container_path_str = str(project_dir.absolute())
        if container_path_str.startswith('/workspace/'):
            # /workspace/context-studio -> $HOST_WORKSPACE_PATH/context-studio
            project_name = container_path_str.replace('/workspace/', '')
            host_project_path = f'{host_workspace}/{project_name}'
        else:
            # Fallback to container path
            host_project_path = container_path_str

        logger.info(f"Mounting project: container={container_path_str}, host={host_project_path}")

        # Get host home directory for SSH/git mounts
        host_home = os.environ.get("HOST_HOME", os.environ.get("HOME", "/root"))

        # Base docker command
        cmd = [
            'docker', 'run',
            '--rm',  # Remove container when done
            '--name', container_name,
            '--network', self.network_name,

            # Mount project directory (using host path)
            '-v', f'{host_project_path}:/workspace',

            # Mount SSH keys for git operations (read-only)
            '-v', f'{host_home}/.ssh:/root/.ssh:ro',

            # Mount git config (read-only)
            '-v', f'{host_home}/.gitconfig:/root/.gitconfig:ro',

            # Working directory inside container
            '-w', '/workspace',

            # Environment variables
            '-e', f'ANTHROPIC_API_KEY={os.environ.get("ANTHROPIC_API_KEY", "")}',
            '-e', f'GITHUB_TOKEN={os.environ.get("GITHUB_TOKEN", "")}',
        ]

        # Special handling for dev_environment_setup agent: mount Docker socket for image building
        if agent == 'dev_environment_setup':
            logger.info("Mounting Docker socket for dev_environment_setup agent (running as root)")
            # On macOS Docker Desktop, socket permissions require root access
            # This is acceptable for dev_environment_setup as it's a build/setup task
            cmd.extend([
                '-v', '/var/run/docker.sock:/var/run/docker.sock',
                '--user', 'root'  # Required for Docker socket access on macOS
            ])

        # Add CONTEXT7_API_KEY if present
        if 'CONTEXT7_API_KEY' in os.environ:
            cmd.extend(['-e', f'CONTEXT7_API_KEY={os.environ["CONTEXT7_API_KEY"]}'])

        # Determine which Docker image to use
        project_name = context.get('project', 'unknown')
        image_name = self._get_image_for_agent(agent, project_name)
        logger.info(f"Using Docker image: {image_name} for agent {agent}")
        cmd.append(image_name)

        return cmd

    def _get_image_for_agent(self, agent: str, project: str) -> str:
        """
        Determine which Docker image to use for an agent

        Args:
            agent: Agent name
            project: Project name

        Returns:
            Docker image name to use
        """
        from config.manager import config_manager
        from services.dev_container_state import dev_container_state

        # Get agent configuration
        agent_config = config_manager.get_project_agent_config(project, agent)
        requires_dev_container = getattr(agent_config, 'requires_dev_container', False)

        if requires_dev_container:
            # Check if project's dev container is verified
            if dev_container_state.is_verified(project):
                # Use project-specific dev container image
                image_name = dev_container_state.get_image_name(project)
                if image_name:
                    logger.info(f"Agent {agent} requires dev container, using project image: {image_name}")
                    return image_name
                else:
                    logger.warning(f"Dev container verified but no image_name found, falling back to orchestrator image")
            else:
                status = dev_container_state.get_status(project)
                logger.warning(f"Agent {agent} requires dev container but project status is {status.value}, using orchestrator image")

        # Default: use orchestrator image
        return 'clauditoreum-orchestrator:latest'

    async def _execute_in_container(
        self,
        docker_cmd: list,
        prompt: str,
        container_name: str,
        stream_callback: Optional[Callable],
        context: Dict[str, Any]
    ) -> str:
        """Execute Claude Code inside the container"""

        # Get configured model from context or use default
        claude_model = context.get('claude_model', 'claude-sonnet-4-5-20250929')
        logger.info(f"Using Claude model in Docker: {claude_model}")

        # Build the Claude command to run inside container
        agent = context.get('agent', 'unknown')
        claude_cmd = [
            'claude',
            '--print',
            '--verbose',
            '--output-format', 'stream-json',
            '--model', claude_model,
        ]

        # Only add --dangerously-skip-permissions if not running as root
        # dev_environment_setup runs as root for Docker access, so skip this flag
        if agent != 'dev_environment_setup':
            claude_cmd.append('--dangerously-skip-permissions')

        claude_cmd.append(prompt)

        # Combine docker command with claude command
        full_cmd = docker_cmd + claude_cmd

        logger.info(f"Executing: docker run ... claude ...")

        obs = context.get('observability')
        agent = context.get('agent', 'unknown')
        task_id = context.get('task_id', 'unknown')
        project = context.get('project', 'unknown')

        # Emit events
        if obs:
            import time
            api_start_time = time.time()
            obs.emit_claude_call_started(agent, task_id, project, claude_model)

        try:
            # Run the container
            process = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL
            )

            # Collect output
            result_parts = []
            input_tokens = 0
            output_tokens = 0

            # Stream output
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                    event_type = event.get('type', 'unknown')

                    # Stream to callback if provided
                    if stream_callback:
                        stream_callback(event)

                    # Track token usage
                    if 'usage' in event:
                        input_tokens = event['usage'].get('input_tokens', input_tokens)
                        output_tokens = event['usage'].get('output_tokens', output_tokens)

                    # Collect result text
                    if event_type == 'assistant':
                        message = event.get('message', {})
                        content = message.get('content', [])
                        for item in content:
                            if isinstance(item, dict) and item.get('type') == 'text':
                                text = item.get('text', '')
                                if text:
                                    result_parts.append(text)
                    elif event_type == 'result':
                        if isinstance(event.get('result'), str):
                            result_parts.append(event['result'])

                except json.JSONDecodeError:
                    logger.debug(f"Non-JSON output: {line[:100]}")

            # Wait for completion
            process.wait(timeout=600)

            # Emit completion
            if obs:
                import time
                api_duration_ms = (time.time() - api_start_time) * 1000
                obs.emit_claude_call_completed(agent, task_id, project, api_duration_ms,
                                               input_tokens, output_tokens)

            if process.returncode == 0:
                result_text = ''.join(result_parts)
                logger.info(f"Agent completed successfully in container, result length: {len(result_text)}")
                return result_text
            else:
                stderr = process.stderr.read() if process.stderr else "Unknown error"
                logger.error(f"Agent failed in container: {stderr}")
                raise Exception(f"Agent execution failed: {stderr}")

        except subprocess.TimeoutExpired:
            logger.error("Agent execution timed out")
            self._cleanup_container(container_name)
            raise Exception("Agent execution timed out")
        except Exception as e:
            logger.error(f"Agent execution error: {e}")
            raise

    def _cleanup_container(self, container_name: str):
        """Clean up the container if it's still running"""
        try:
            subprocess.run(
                ['docker', 'rm', '-f', container_name],
                capture_output=True,
                timeout=10
            )
            logger.debug(f"Cleaned up container {container_name}")
        except Exception as e:
            logger.warning(f"Failed to cleanup container {container_name}: {e}")


# Global instance
docker_runner = DockerAgentRunner()
