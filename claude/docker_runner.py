import subprocess
import json
import logging
import os
import re
from typing import Dict, Any, Optional, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


class DockerAgentRunner:
    """Runs Claude Code agents in isolated Docker containers"""

    def __init__(self, network_name: str = "clauditoreum_orchestrator-net"):
        # Docker Compose prefixes network names with project directory name
        self.network_name = network_name

    @staticmethod
    def _sanitize_container_name(name: str) -> str:
        """
        Sanitize container name to only contain valid Docker container name characters.

        Docker allows: [a-zA-Z0-9][a-zA-Z0-9_.-]

        Args:
            name: Raw container name that may contain invalid characters

        Returns:
            Sanitized container name with invalid characters replaced by dashes
        """
        # Replace any character that's not alphanumeric, underscore, period, or dash
        sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '-', name)

        # Ensure it starts with alphanumeric (not dash/period)
        sanitized = re.sub(r'^[^a-zA-Z0-9]+', '', sanitized)

        # Remove any consecutive dashes
        sanitized = re.sub(r'-+', '-', sanitized)

        return sanitized

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
        raw_container_name = f"claude-agent-{project}-{task_id}"
        container_name = self._sanitize_container_name(raw_container_name)

        # Determine if we'll need stdin (for large prompts)
        use_stdin = len(prompt) > 50000

        docker_cmd = self._build_docker_command(
            container_name=container_name,
            project_dir=project_dir,
            mcp_config=mcp_config,
            context=context,
            use_stdin=use_stdin
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
        context: Dict[str, Any],
        use_stdin: bool = False
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

        # Get agent config to check filesystem write permission
        from config.manager import config_manager
        agent_config = config_manager.get_project_agent_config(context.get('project', 'unknown'), agent)
        filesystem_write_allowed = getattr(agent_config, 'filesystem_write_allowed', True)

        # Determine mount mode for workspace
        workspace_mount_mode = 'rw' if filesystem_write_allowed else 'ro'
        if not filesystem_write_allowed:
            logger.warning(f"Agent {agent} has filesystem_write_allowed=false, mounting workspace as READ-ONLY")

        # Get host home directory for SSH/git mounts
        host_home = os.environ.get("HOST_HOME", os.environ.get("HOME", "/root"))

        # Base docker command - build list dynamically to conditionally add -i flag
        cmd = ['docker', 'run', '--rm', '--name', container_name, '--network', self.network_name]

        # Add -i (interactive) flag if we need stdin for large prompts
        if use_stdin:
            cmd.append('-i')
            logger.info("Adding -i flag for stdin support")

        # Add volume mounts, working directory, and environment variables
        cmd.extend([
            # Mount project directory (read-only or read-write based on config)
            '-v', f'{host_project_path}:/workspace:{workspace_mount_mode}',

            # Mount SSH keys for git operations (read-only)
            '-v', f'{host_home}/.ssh:/root/.ssh:ro',

            # Mount git config (read-only)
            '-v', f'{host_home}/.gitconfig:/root/.gitconfig:ro',

            # Working directory inside container
            '-w', '/workspace',

            # Environment variables
            '-e', f'GITHUB_TOKEN={os.environ.get("GITHUB_TOKEN", "")}',
        ])

        # Pass CLAUDE_CODE_OAUTH_TOKEN if available (subscription), else ANTHROPIC_API_KEY (pay-per-use)
        oauth_token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '').strip()
        anthropic_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()

        if oauth_token:
            cmd.extend(['-e', f'CLAUDE_CODE_OAUTH_TOKEN={oauth_token}'])
            logger.info("Using CLAUDE_CODE_OAUTH_TOKEN (subscription billing)")
        elif anthropic_key:
            cmd.extend(['-e', f'ANTHROPIC_API_KEY={anthropic_key}'])
            logger.info("Using ANTHROPIC_API_KEY (API billing)")
        else:
            logger.warning("No authentication token found - agent will likely fail")

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

        # For large prompts, write to a temp file and mount it into container
        import tempfile
        import os

        prompt_file = None
        use_file_mount = len(prompt) > 50000  # Use file mount for prompts > 50KB

        if use_file_mount:
            # Create a temporary file for the prompt
            prompt_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', dir='/tmp')
            prompt_file.write(prompt)
            prompt_file.close()
            logger.info(f"Prompt is large ({len(prompt)} chars), using mounted file: {prompt_file.name}")

        # Check for existing session to resume
        existing_session_id = context.get('claude_session_id')

        claude_cmd = [
            'claude',
            '--print',
            '--verbose',
            '--output-format', 'stream-json',
            '--model', claude_model,
        ]

        # Choose permission mode based on whether running as root
        # bypassPermissions is blocked when running as root, so use acceptEdits instead
        if agent == 'dev_environment_setup':
            # dev_environment_setup runs as root (needs Docker socket), use acceptEdits
            claude_cmd.extend(['--permission-mode', 'acceptEdits'])
            logger.info("Using --permission-mode acceptEdits (running as root)")
        else:
            # Other agents run as non-root, can use bypassPermissions
            claude_cmd.extend(['--permission-mode', 'bypassPermissions'])
            logger.info("Using --permission-mode bypassPermissions")

        # Add --resume flag if continuing an existing session
        if existing_session_id:
            claude_cmd.extend(['--resume', existing_session_id])
            logger.info(f"Resuming Claude Code session: {existing_session_id}")

        if use_file_mount:
            # Large prompt - will be sent via stdin
            # Use '-' to tell claude to read from stdin
            claude_cmd.append('-')
        else:
            # Small prompt - pass directly
            claude_cmd.append(prompt)

        # Combine docker command with claude command
        full_cmd = docker_cmd + claude_cmd

        logger.info(f"Executing: docker run ... claude ...")
        logger.info(f"Full command (sanitized): {' '.join(full_cmd[:10])} ... {' '.join(full_cmd[-5:])}")

        obs = context.get('observability')
        agent = context.get('agent', 'unknown')
        task_id = context.get('task_id', 'unknown')
        project = context.get('project', 'unknown')

        # Emit events
        if obs:
            import time
            api_start_time = time.time()
            obs.emit_claude_call_started(agent, task_id, project, claude_model)

        prompt_input = None
        try:
            # Prepare stdin if using prompt file
            if use_file_mount and prompt_file:
                with open(prompt_file.name, 'r') as f:
                    prompt_input = f.read()
                logger.info(f"DOCKER DEBUG - Prompt file size: {len(prompt_input)} chars")
                logger.info(f"DOCKER DEBUG - Prompt starts with: {prompt_input[:200]}")

            # Run the container
            if use_file_mount and prompt_input:
                # For stdin input, use communicate() to avoid broken pipe
                process = subprocess.Popen(
                    full_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True
                )
                # Note: We'll handle stdin in the streaming loop below
            else:
                process = subprocess.Popen(
                    full_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    stdin=subprocess.DEVNULL
                )

            # Collect output
            result_parts = []
            stderr_parts = []
            input_tokens = 0
            output_tokens = 0
            session_id = None  # Track session ID for continuity

            # Read both stdout and stderr using threads to avoid deadlock
            import threading

            def read_stderr():
                for line in iter(process.stderr.readline, ''):
                    if line:
                        stderr_parts.append(line)
                        logger.error(f"Container stderr: {line.strip()}")

            def write_stdin():
                """Write prompt to stdin in a separate thread"""
                if use_file_mount and prompt_input and process.stdin:
                    try:
                        logger.info(f"STDIN DEBUG - Writing {len(prompt_input)} chars to stdin")
                        process.stdin.write(prompt_input)
                        process.stdin.flush()
                        logger.info("STDIN DEBUG - Stdin write completed, closing stdin")
                        process.stdin.close()
                        logger.info("STDIN DEBUG - Stdin closed successfully")
                    except BrokenPipeError:
                        logger.warning("STDIN DEBUG - Broken pipe while writing to stdin (process may have exited early)")
                    except Exception as e:
                        logger.error(f"STDIN DEBUG - Error writing to stdin: {e}")

            # Start stderr reader thread
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()

            # Start stdin writer thread if needed
            if use_file_mount and prompt_input:
                stdin_thread = threading.Thread(target=write_stdin, daemon=True)
                stdin_thread.start()

            # Stream stdout
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                    event_type = event.get('type', 'unknown')

                    # Capture session_id for session continuity
                    if 'session_id' in event and not session_id:
                        session_id = event['session_id']
                        logger.info(f"Captured Claude Code session_id: {session_id}")

                    # Stream to callback if provided
                    if stream_callback:
                        stream_callback(event)

                    # Track token usage
                    if 'usage' in event:
                        input_tokens = event['usage'].get('input_tokens', input_tokens)
                        output_tokens = event['usage'].get('output_tokens', output_tokens)

                    # Collect result text from assistant messages only
                    # Note: Do not collect from 'result' events as they duplicate the assistant content
                    if event_type == 'assistant':
                        message = event.get('message', {})
                        content = message.get('content', [])
                        for item in content:
                            if isinstance(item, dict) and item.get('type') == 'text':
                                text = item.get('text', '')
                                if text:
                                    result_parts.append(text)

                except json.JSONDecodeError:
                    # Non-JSON output might be error messages
                    logger.warning(f"Non-JSON stdout: {line}")
                    stderr_parts.append(f"STDOUT: {line}\n")

            # Wait for completion
            process.wait(timeout=600)

            # Wait for stderr thread to finish
            stderr_thread.join(timeout=1)

            # Emit completion
            if obs:
                import time
                api_duration_ms = (time.time() - api_start_time) * 1000
                obs.emit_claude_call_completed(agent, task_id, project, api_duration_ms,
                                               input_tokens, output_tokens)

            if process.returncode == 0:
                result_text = ''.join(result_parts)
                logger.info(f"Agent completed successfully in container, result length: {len(result_text)}, session_id: {session_id}")

                # Store session_id in context for session continuity
                if session_id:
                    context['claude_session_id'] = session_id

                # Return just the result text (callers expect string, not dict)
                return result_text
            else:
                stderr_text = ''.join(stderr_parts) if stderr_parts else "No error output captured"
                logger.error(f"Agent failed in container (returncode={process.returncode}): {stderr_text}")
                raise Exception(f"Agent execution failed (returncode={process.returncode}): {stderr_text}")

        except subprocess.TimeoutExpired:
            logger.error("Agent execution timed out")
            self._cleanup_container(container_name)
            raise Exception("Agent execution timed out")
        except Exception as e:
            logger.error(f"Agent execution error: {e}")
            raise
        finally:
            # Clean up temp prompt file
            if prompt_file and os.path.exists(prompt_file.name):
                try:
                    os.unlink(prompt_file.name)
                    logger.debug(f"Cleaned up temp prompt file: {prompt_file.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temp file: {e}")

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
