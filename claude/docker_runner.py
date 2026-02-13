import subprocess
import json
import logging
import os
import re
import threading
from typing import Dict, Any, Optional, Callable
from pathlib import Path
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Import Claude Code breaker for token limit detection
try:
    from monitoring.claude_code_breaker import get_breaker
except ImportError:
    # Fallback if module not available
    def get_breaker():
        return None


class DockerAgentRunner:
    """Runs Claude Code agents in isolated Docker containers"""

    def __init__(self, network_name: str = "clauditoreum_orchestrator-net"):
        # Docker Compose prefixes network names with project directory name
        self.network_name = network_name
        self._host_workspace_path = None  # Cache for host workspace path detection
        self._redis = None  # Lazy Redis connection (shared across register/unregister/persist calls)

    def _get_redis(self):
        """Lazy Redis connection (tolerates Redis being unavailable)."""
        if self._redis is None:
            try:
                import redis
                self._redis = redis.Redis(host='redis', port=6379, decode_responses=True)
                self._redis.ping()
            except Exception as e:
                logger.warning(f"Redis unavailable for container tracking: {e}")
                self._redis = None
        return self._redis

    @staticmethod
    def _detect_host_workspace_path() -> str:
        """
        Auto-detect the actual host filesystem path that is mounted to /workspace.
        
        This reads /proc/self/mountinfo to find the real host path, avoiding issues
        with Snap Docker where $HOME environment variable points to snap-specific paths
        like /home/user/snap/docker/XXX instead of /home/user.
        
        Returns:
            The host filesystem path mounted to /workspace, or '/workspace' as fallback
        """
        try:
            with open('/proc/self/mountinfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    # Format: mount_id parent_id major:minor root mount_point ...
                    if len(parts) >= 5:
                        mount_point = parts[4]  # The mount point in container namespace
                        if mount_point == '/workspace':
                            # The 'root' field contains the host path
                            host_path = parts[3]
                            logger.info(f"Auto-detected host workspace path: {host_path}")
                            return host_path
        except Exception as e:
            logger.warning(f"Failed to auto-detect host workspace path: {e}")
        
        # Fallback to environment variable or default
        fallback = os.environ.get('HOST_WORKSPACE_PATH', '/workspace')
        logger.info(f"Using fallback host workspace path: {fallback}")
        return fallback

    @staticmethod
    def _detect_host_home_path() -> str:
        """
        Auto-detect the actual host home directory path.
        
        This reads /proc/self/mountinfo to find the host's home directory from SSH key mounts,
        avoiding issues with Snap Docker where $HOME points to snap-specific paths.
        
        Returns:
            The host home directory path, or $HOME as fallback
        """
        try:
            with open('/proc/self/mountinfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 5:
                        mount_point = parts[4]
                        # Look for .ssh mount which should be from host home
                        if '.ssh' in mount_point:
                            host_path = parts[3]  # e.g., /home/username/.ssh/id_ed25519
                            # Extract home directory (remove /.ssh/... part)
                            if '/.ssh/' in host_path:
                                host_home = host_path.split('/.ssh/')[0]
                                logger.info(f"Auto-detected host home path: {host_home}")
                                return host_home
        except Exception as e:
            logger.warning(f"Failed to auto-detect host home path: {e}")
        
        # Fallback to environment variable or container's HOME
        fallback = os.environ.get("HOST_HOME", os.environ.get("HOME", "/root"))
        logger.info(f"Using fallback host home path: {fallback}")
        return fallback

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

    @staticmethod
    def _verify_write_access(directory_path: str) -> Dict[str, Any]:
        """
        Verify that we have write access to a directory by attempting to create and delete a test file.

        Args:
            directory_path: Path to directory to test (on host filesystem)

        Returns:
            Dict with 'success' (bool), 'message' (str), and 'error' (str) if failed
        """
        import tempfile
        import time

        try:
            # Create a unique test filename
            test_filename = f".write-test-{int(time.time())}.tmp"
            test_path = os.path.join(directory_path, test_filename)

            # Try to write the file
            with open(test_path, 'w') as f:
                f.write("write test")

            # Try to read it back
            with open(test_path, 'r') as f:
                content = f.read()
                if content != "write test":
                    return {
                        'success': False,
                        'error': f"Write verification failed: content mismatch (wrote 'write test', read '{content}')"
                    }

            # Try to delete it
            os.remove(test_path)

            return {
                'success': True,
                'message': f"Can read, write, and delete files in {directory_path}"
            }

        except PermissionError as e:
            return {
                'success': False,
                'error': f"Permission denied: {e}"
            }
        except Exception as e:
            return {
                'success': False,
                'error': f"Unexpected error: {e}"
            }

    async def _verify_container_write_access(
        self,
        docker_cmd: list,
        agent: str,
        project: str
    ) -> bool:
        """
        Spawn a test container with the same configuration and verify it can write to /workspace.

        This is a critical safety check that runs before launching the expensive Claude agent.
        It catches permission issues immediately instead of wasting time and API calls.

        Args:
            docker_cmd: The docker run command being prepared (should be ['docker', 'run', '--rm', '--name', name, ...])
            agent: Agent name
            project: Project name

        Returns:
            True if container can write, False otherwise
        """
        import subprocess
        import time
        import uuid
        import asyncio

        # Retry configuration
        max_retries = 3
        retry_delay = 2

        for attempt in range(1, max_retries + 1):
            # Create a unique test filename to avoid race conditions
            unique_id = str(uuid.uuid4())[:8]
            test_filename = f".write-verify-{unique_id}"
            test_container_name = f"write-test-{agent}-{unique_id}"

            # docker_cmd ends with the image name - remove it and everything after
            # docker_cmd structure: ['docker', 'run', '--rm', '--name', 'name', ..., 'image']
            test_cmd = docker_cmd[:-1]  # Remove the image name

            # Remove all -e environment variables to avoid gh auth warnings in write test
            filtered_cmd = []
            skip_next = False
            for i, arg in enumerate(test_cmd):
                if skip_next:
                    skip_next = False
                    continue
                if arg == '-e':
                    skip_next = True  # Skip the next argument (the env var value)
                    continue
                filtered_cmd.append(arg)
            test_cmd = filtered_cmd

            # Replace the container name if it exists
            if '--name' in test_cmd:
                name_index = test_cmd.index('--name')
                test_cmd[name_index + 1] = test_container_name
            else:
                # Add --name after --rm if it doesn't exist
                for i, arg in enumerate(test_cmd):
                    if arg == '--rm':
                        test_cmd.insert(i + 1, '--name')
                        test_cmd.insert(i + 2, test_container_name)
                        break

            # Add the image and simple test command
            # Test writes to /workspace (the project directory), NOT to /home/orchestrator/.ssh (which is read-only)
            # Use unique filename to prevent race conditions between concurrent agents
            test_cmd.extend([
                'clauditoreum-orchestrator:latest',  # Use simple base image for quick test
                'sh', '-c',
                f'echo "test" > /workspace/{test_filename} && cat /workspace/{test_filename} && rm /workspace/{test_filename} && echo "SUCCESS"'
            ])

            logger.info(f"   Running container write test (attempt {attempt}/{max_retries}) with container: {test_container_name}")

            try:
                result = subprocess.run(
                    test_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30  # Increased from 10s to 30s for reliability
                )

                if result.returncode == 0 and 'SUCCESS' in result.stdout:
                    logger.info(f"   ✓ Container write test PASSED")
                    return True
                else:
                    logger.warning(f"   ✗ Container write test FAILED (attempt {attempt}/{max_retries})")
                    logger.warning(f"   Return code: {result.returncode}")
                    logger.warning(f"   Stdout: {result.stdout}")
                    logger.warning(f"   Stderr: {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                logger.warning(f"   ✗ Container write test TIMED OUT (attempt {attempt}/{max_retries})")
                # Try to clean up
                subprocess.run(['docker', 'rm', '-f', test_container_name], capture_output=True)
                
            except Exception as e:
                logger.warning(f"   ✗ Container write test ERROR (attempt {attempt}/{max_retries}): {e}")

            # Wait before retry if not last attempt
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)

        logger.error(f"   ✗ Container write test FAILED after {max_retries} attempts")
        return False

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

        # Check Claude Code circuit breaker before launching agent
        breaker = get_breaker()
        if breaker and breaker.is_open():
            can_execute, error_msg = (False, "Circuit breaker open")
        else:
            can_execute, error_msg = (True, None)

        if not can_execute:
            from monitoring.claude_code_breaker import check_breaker_before_agent_execution
            can_execute, error_msg = check_breaker_before_agent_execution(agent)
            if not can_execute:
                logger.error(f"Agent execution blocked: {error_msg}")
                raise Exception(error_msg)

        # Prepare MCP configuration if needed
        mcp_config = None
        mcp_config_path = None
        if mcp_servers and len(mcp_servers) > 0:
            mcp_config = self._prepare_mcp_config(mcp_servers, project_dir)
            # Write MCP config to a temp file
            mcp_config_path = self._write_mcp_config_file(mcp_config, agent, task_id)

        # Build docker run command
        raw_container_name = f"claude-agent-{project}-{task_id}"
        container_name = self._sanitize_container_name(raw_container_name)

        # Always use stdin for passing prompts (avoids command-line length limits and escaping issues)
        use_stdin = True

        docker_cmd, image_name = self._build_docker_command(
            container_name=container_name,
            project_dir=project_dir,
            mcp_config_path=mcp_config_path,
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
                context=context,
                project_dir=project_dir,
                image_name=image_name,
                mcp_config_path=mcp_config_path
            )

            return result_text

        finally:
            # Clean up container and tracking
            self._cleanup_container(container_name)
            self._unregister_active_container(container_name)
            # Clean up MCP config file
            if mcp_config_path and os.path.exists(mcp_config_path):
                try:
                    os.remove(mcp_config_path)
                    logger.debug(f"Cleaned up MCP config file: {mcp_config_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up MCP config file: {e}")

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

    def _write_mcp_config_file(self, mcp_config: Dict, agent: str, task_id: str) -> str:
        """
        Write MCP configuration to a temporary JSON file.

        Args:
            mcp_config: The MCP configuration dict
            agent: Agent name (for logging)
            task_id: Task ID (for unique filename)

        Returns:
            Path to the created MCP config file
        """
        import tempfile
        import time

        timestamp = int(time.time())

        # Defensive validation: Sanitize task_id to prevent Docker mount parsing errors
        # Docker interprets colons (:) as volume mount separators, causing "too many colons" errors
        if ':' in task_id:
            original_task_id = task_id
            task_id = task_id.replace(':', '_')
            logger.warning(
                f"Task ID contained colon (Docker mount separator), sanitized for safety: "
                f"'{original_task_id}' -> '{task_id}'"
            )

        # Additional sanitization for other problematic characters in filenames
        problematic_chars = ['/', '\\', ' ', '*', '?', '"', '<', '>', '|']
        for char in problematic_chars:
            if char in task_id:
                task_id = task_id.replace(char, '_')

        mcp_config_filename = f"mcp_config_{agent}_{task_id}_{timestamp}.json"

        # Try to find a writable directory
        # Priority 1: /workspace/.orchestrator/tmp (Shared volume, preferred)
        # Priority 2: /app/tmp (Legacy, might be read-only)
        # Priority 3: Local tmp (Fallback)
        
        candidate_dirs = []
        if os.path.exists("/workspace"):
            candidate_dirs.append("/workspace/.orchestrator/tmp")
        if os.path.exists("/app"):
            candidate_dirs.append("/app/tmp")
        
        # Fallbacks
        candidate_dirs.append(os.path.join(os.getcwd(), "tmp"))
        candidate_dirs.append("/tmp")

        selected_path = None

        for temp_dir in candidate_dirs:
            try:
                os.makedirs(temp_dir, exist_ok=True)
                test_path = os.path.join(temp_dir, f".write_test_{timestamp}")
                
                # Verify write access
                with open(test_path, 'w') as f:
                    f.write("test")
                os.remove(test_path)
                
                # If successful, use this directory
                selected_path = os.path.join(temp_dir, mcp_config_filename)
                logger.info(f"Using temporary directory for MCP config: {temp_dir}")
                break
            except Exception as e:
                logger.debug(f"Skipping temporary directory {temp_dir}: {e}")
                continue

        if not selected_path:
            # Last resort fallback
            selected_path = os.path.join("/tmp", mcp_config_filename)
            logger.warning(f"Could not verify write access to any temp dir, falling back to {selected_path}")

        # Write the config
        try:
            with open(selected_path, 'w') as f:
                json.dump(mcp_config, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write MCP config to {selected_path}: {e}")
            raise

        logger.info(f"Wrote MCP config to {selected_path}")
        logger.debug(f"MCP config contents: {json.dumps(mcp_config, indent=2)}")

        return selected_path

    def _build_docker_command(
        self,
        container_name: str,
        project_dir: Path,
        mcp_config_path: Optional[str],
        context: Dict[str, Any],
        use_stdin: bool = False
    ) -> tuple[list, str]:
        """Build the docker run command"""

        agent = context.get('agent', 'unknown')

        # Convert container project path to host path for Docker-in-Docker
        # In docker-compose.yml: - ..:/workspace means /workspace maps to host's ~/workspace/orchestrator
        
        # Auto-detect the actual host path by reading /proc/self/mountinfo
        # This avoids issues with Snap Docker where $HOME points to snap-specific paths
        host_workspace = self._detect_host_workspace_path()
        
        container_path_str = str(project_dir.absolute())
        if container_path_str.startswith('/workspace/'):
            # /workspace/project-name -> $HOST_WORKSPACE_PATH/project-name
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
        else:
            logger.info(f"Agent {agent} has filesystem_write_allowed=true, mounting workspace as READ-WRITE")

            # Verify write access if agent expects it
            # Use container path (project_dir) not host path for verification
            if filesystem_write_allowed:
                logger.info(f"Verifying write access to {project_dir}")
                test_result = self._verify_write_access(str(project_dir))
                if not test_result['success']:
                    logger.error(f"FILESYSTEM WRITE TEST FAILED: {test_result['error']}")
                    logger.error(f"Agent {agent} expects write access but cannot write to workspace!")
                    raise Exception(f"Filesystem write verification failed: {test_result['error']}")
                else:
                    logger.info(f"✓ Filesystem write verification passed: {test_result['message']}")

        # Get host home directory for SSH/git mounts (auto-detect to avoid Snap Docker issues)
        host_home = self._detect_host_home_path()

        # Base docker command - build list dynamically to conditionally add -i flag
        cmd = ['docker', 'run', '--rm', '--name', container_name, '--network', self.network_name]

        # Add labels for robust recovery
        # These labels allow the recovery system to identify containers without parsing names
        # and without relying on Redis state that might be lost during a crash.
        # Resolve execution_type from nested task_context or top-level context
        task_context_for_labels = context.get('context', {})
        execution_type_label = task_context_for_labels.get('execution_type') or context.get('execution_type', '')

        cmd.extend([
            '--label', f'org.clauditoreum.project={context.get("project", "unknown")}',
            '--label', f'org.clauditoreum.agent={agent}',
            '--label', f'org.clauditoreum.task_id={context.get("task_id", "unknown")}',
            '--label', f'org.clauditoreum.execution_type={execution_type_label}',
            '--label', 'org.clauditoreum.managed=true'
        ])

        # Add optional labels if available
        # Note: issue_number and pipeline_run_id live inside the nested task_context
        # (context['context']), not at the top level. Check both for backwards compatibility.
        task_context = context.get('context', {})
        issue_number = task_context.get('issue_number') or context.get('issue_number')
        if issue_number:
            cmd.extend(['--label', f'org.clauditoreum.issue_number={issue_number}'])

        pipeline_run_id = task_context.get('pipeline_run_id') or context.get('pipeline_run_id')
        if pipeline_run_id:
            cmd.extend(['--label', f'org.clauditoreum.pipeline_run_id={pipeline_run_id}'])

        # Add -i (interactive) flag if we need stdin for large prompts
        if use_stdin:
            cmd.append('-i')
            logger.info("Adding -i flag for stdin support")

        # Add volume mounts, working directory, and environment variables
        mount_spec = f'{host_project_path}:/workspace:{workspace_mount_mode}'
        logger.info(f"DOCKER MOUNT: {mount_spec}")

        cmd.extend([
            # Mount project directory (read-only or read-write based on config)
            '-v', mount_spec,

            # Mount SSH keys for git operations (read-only)
            '-v', f'{host_home}/.ssh:/home/orchestrator/.ssh:ro',

            # Mount git config
            '-v', f'{host_home}/.gitconfig:/home/orchestrator/.gitconfig',
        ])

        # Mount Claude Code wrapper script for container-side Redis writes
        wrapper_host_path = f'{host_workspace}/clauditoreum/scripts/docker-claude-wrapper.py'
        cmd.extend([
            '-v', f'{wrapper_host_path}:/app/scripts/docker-claude-wrapper.py:ro'
        ])
        logger.info(f"Mounting wrapper: {wrapper_host_path} -> /app/scripts/docker-claude-wrapper.py")

        # Claude Code plugins, agents, commands, and skills are baked into the Docker image
        # via the Dockerfile. Runtime state (todos, debug, etc.) writes to the container's
        # writable layer, which is cleaned up automatically since containers run with --rm.

        # Mount MCP config file if provided (task-specific MCP servers)
        # The MCP config is written to a temp location accessible from host
        # We need to convert container path to host path for Docker-in-Docker mounting
        if mcp_config_path:
            host_mcp_path = mcp_config_path

            if mcp_config_path.startswith('/app/'):
                # Running in orchestrator - /app is mounted from host
                # Convert /app/tmp/file.json -> host_workspace/clauditoreum/tmp/file.json
                relative_from_app = mcp_config_path.replace('/app/', '')
                host_mcp_path = f'{host_workspace}/clauditoreum/{relative_from_app}'
            
            elif mcp_config_path.startswith('/workspace/'):
                # Running in orchestrator - /workspace is mounted from host
                # Convert /workspace/.orchestrator/tmp/file.json -> host_workspace/.orchestrator/tmp/file.json
                relative_from_workspace = mcp_config_path.replace('/workspace/', '')
                host_mcp_path = f'{host_workspace}/{relative_from_workspace}'
            
            # Else: Running locally or absolute path - use as-is (assuming shared filesystem or local docker)

            # Mount to /home/orchestrator/.mcp_config.json to ensure parent dir exists
            cmd.extend(['-v', f'{host_mcp_path}:/home/orchestrator/.mcp_config.json:ro'])
            logger.info(f"Mounting MCP config: {host_mcp_path} -> /home/orchestrator/.mcp_config.json")

        cmd.extend([
            # Working directory inside container
            '-w', '/workspace',

            # Environment variables
            '-e', f'GITHUB_TOKEN={os.environ.get("GITHUB_TOKEN", "")}',
            '-e', 'GH_AUTH_SETUP_REQUIRED=true',
            '-e', 'PYTHONDONTWRITEBYTECODE=1',
        ])
        
        # Pass PIPELINE_RUN_ID if available in context (for event tracking)
        pipeline_run_id = context.get('pipeline_run_id')
        if pipeline_run_id:
            cmd.extend(['-e', f'PIPELINE_RUN_ID={pipeline_run_id}'])
            logger.info(f"Passing PIPELINE_RUN_ID={pipeline_run_id} to agent container")

        # Pass CLAUDE_CODE_OAUTH_TOKEN if available (subscription), else ANTHROPIC_API_KEY (pay-per-use)
        oauth_token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '').strip()
        anthropic_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()

        if oauth_token:
            cmd.extend(['-e', f'CLAUDE_CODE_OAUTH_TOKEN={oauth_token}'])
            logger.info("Using CLAUDE_CODE_OAUTH_TOKEN (subscription billing)")
            logger.info(f"DEBUG: OAuth token length: {len(oauth_token)}, starts with: {oauth_token[:10]}...")
        elif anthropic_key:
            cmd.extend(['-e', f'ANTHROPIC_API_KEY={anthropic_key}'])
            logger.info("Using ANTHROPIC_API_KEY (API billing)")
        else:
            logger.warning("No authentication token found - agent will likely fail")

        # Redis connection info for direct container writes (container-side Redis communication)
        # Note: issue_number lives inside nested task_context, not top-level context
        task_ctx = context.get('context', {})
        resolved_issue_number = task_ctx.get('issue_number') or context.get('issue_number', 'unknown')
        cmd.extend([
            '-e', 'REDIS_HOST=redis',
            '-e', 'REDIS_PORT=6379',
            '-e', f'AGENT={agent}',
            '-e', f'TASK_ID={context.get("task_id", "unknown")}',
            '-e', f'PROJECT={context.get("project", "unknown")}',
            '-e', f'ISSUE_NUMBER={resolved_issue_number}',
        ])

        # Special handling for dev_environment_setup agent: mount Docker socket for image building
        if agent == 'dev_environment_setup':
            logger.info("Mounting Docker socket for dev_environment_setup agent")
            # The orchestrator user is already part of the docker group (see Dockerfile)
            # This allows Docker socket access without requiring root
            cmd.extend([
                '-v', '/var/run/docker.sock:/var/run/docker.sock',
            ])

        # Note: With rootful Docker, no user namespace remapping by default
        # Container UID 1000 = host UID 1000 directly, giving proper file access
        # and allowing Claude Code to run with bypass permissions (not as root)

        # Add CONTEXT7_API_KEY if present
        if 'CONTEXT7_API_KEY' in os.environ:
            cmd.extend(['-e', f'CONTEXT7_API_KEY={os.environ["CONTEXT7_API_KEY"]}'])

        # Determine which Docker image to use
        project_name = context.get('project', 'unknown')
        image_name = self._get_image_for_agent(agent, project_name)
        logger.info(f"Using Docker image: {image_name} for agent {agent}")
        cmd.append(image_name)

        return cmd, image_name

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

    def _detect_rate_limit_reset_time(self, project_dir: Path) -> Optional[datetime]:
        """
        Detect Claude Code rate limit reset time by querying Elasticsearch for recent rate limit errors.

        Claude Code logs rate limit errors to claude-streams-* indices with:
        - raw_event.event.error: "rate_limit"
        - raw_event.event.message.content: [{type: "text", text: "You've hit your limit · resets 5pm (UTC)"}]

        This method queries the structured logs which contain precise error classification and reset times,
        instead of trying to parse ephemeral stdout/stderr which often produces no output.

        Args:
            project_dir: Project directory path (unused, kept for signature compatibility)

        Returns:
            Datetime when rate limit resets, or None if no rate limit detected
        """
        try:
            from elasticsearch import Elasticsearch

            # Connect to Elasticsearch
            es_host = os.environ.get('ELASTICSEARCH_HOST', 'elasticsearch')
            es_port = os.environ.get('ELASTICSEARCH_PORT', '9200')
            es = Elasticsearch([f"http://{es_host}:{es_port}"])

            # Query for rate limit errors in the last 5 minutes
            now_utc = datetime.now(timezone.utc)
            five_min_ago = now_utc - timedelta(minutes=5)

            query = {
                "size": 5,
                "sort": [{"timestamp": "desc"}],
                "query": {
                    "bool": {
                        "must": [
                            {
                                "range": {
                                    "timestamp": {
                                        "gte": five_min_ago.isoformat(),
                                        "lte": now_utc.isoformat()
                                    }
                                }
                            },
                            {
                                "term": {
                                    "raw_event.event.error.keyword": "rate_limit"
                                }
                            }
                        ]
                    }
                }
            }

            result = es.search(index="claude-streams-*", body=query)
            hits = result.get("hits", {}).get("hits", [])

            if not hits:
                logger.debug("No rate limit errors found in Elasticsearch (last 5 minutes)")
                return None

            # Extract the most recent rate limit event
            event = hits[0]["_source"]["raw_event"]["event"]
            message_content = event.get("message", {}).get("content", [])

            # Find the text content with the reset time
            limit_message = None
            for content in message_content:
                if content.get("type") == "text":
                    limit_message = content.get("text", "")
                    break

            if not limit_message:
                logger.warning("Rate limit event found but no message text")
                return None

            logger.info(f"✓ Found rate limit error in Elasticsearch: {limit_message}")

            # Parse reset time from message using circuit breaker's parser
            breaker = get_breaker()
            if breaker:
                is_limit, reset_time = breaker.detect_session_limit(limit_message)
                if is_limit and reset_time:
                    logger.info(f"✓ Detected rate limit reset time from Elasticsearch: {reset_time}")
                    return reset_time

            logger.warning(f"Could not parse reset time from message: {limit_message}")
            return None

        except Exception as e:
            logger.warning(f"Failed to detect rate limit from Elasticsearch: {e}")
            return None

    async def _execute_in_container(
        self,
        docker_cmd: list,
        prompt: str,
        container_name: str,
        stream_callback: Optional[Callable],
        context: Dict[str, Any],
        project_dir: Path,
        image_name: str,
        mcp_config_path: Optional[str] = None
    ) -> str:
        """Execute Claude Code inside the container"""

        # Get configured model from context or use default
        claude_model = context.get('claude_model', 'claude-sonnet-4-5-20250929')
        logger.info(f"Using Claude model in Docker: {claude_model}")

        # Get agent info for safety check
        agent = context.get('agent', 'unknown')
        task_id = context.get('task_id', 'unknown')
        from config.manager import config_manager
        agent_config = config_manager.get_project_agent_config(context.get('project', 'unknown'), agent)
        filesystem_write_allowed = getattr(agent_config, 'filesystem_write_allowed', True)

        # SAFETY CHECK: Verify container can actually write to workspace before launching expensive agent
        if filesystem_write_allowed:
            logger.info("Pre-launch safety check: Verifying container write access...")
            write_test_passed = await self._verify_container_write_access(
                docker_cmd=docker_cmd,
                agent=agent,
                project=context.get('project', 'unknown')
            )
            if not write_test_passed:
                logger.error("FATAL: Container cannot write to workspace - aborting agent launch")
                raise Exception("Container write access verification failed - agent would not be able to write files")
            logger.info("✓ Pre-launch verification passed: Container has write access")

        # Build the Claude command to run inside container
        # We use file-based input/output to support detached execution and orchestrator restarts
        
        # Sanitize task_id for filename (replace spaces and special chars)
        safe_task_id = re.sub(r'[^a-zA-Z0-9_-]', '_', str(task_id))
        
        # Write prompt to file in project directory (mounted in container)
        prompt_filename = f".claude_prompt_{safe_task_id}.txt"
        prompt_path = project_dir / prompt_filename
        try:
            with open(prompt_path, 'w') as f:
                f.write(prompt)
            logger.info(f"Wrote prompt to {prompt_path} ({len(prompt)} chars)")
        except Exception as e:
            logger.error(f"Failed to write prompt file: {e}")
            raise

        # Check for existing session to resume
        existing_session_id = context.get('claude_session_id')

        # DEBUG: Verify file existence
        try:
            if os.path.exists(prompt_path):
                logger.info(f"DEBUG: Prompt file exists at {prompt_path}")
                stat = os.stat(prompt_path)
                logger.info(f"DEBUG: File size: {stat.st_size}, permissions: {oct(stat.st_mode)}")
            else:
                logger.error(f"DEBUG: Prompt file DOES NOT EXIST at {prompt_path}")
        except Exception as e:
            logger.error(f"DEBUG: Error checking prompt file: {e}")

        claude_cmd = [
            'claude',
            '--print',
            '--verbose',
            '--output-format', 'stream-json',
            '--model', claude_model,
        ]

        # Add MCP config if provided
        if mcp_config_path:
            # Always use the fixed mount point inside the agent container
            claude_cmd.extend(['--mcp-config', '/home/orchestrator/.mcp_config.json'])
            logger.info("Using MCP config: /home/orchestrator/.mcp_config.json")

        # Use bypassPermissions for all agents in containerized environment
        claude_cmd.extend(['--permission-mode', 'bypassPermissions'])

        # Add --resume flag if continuing an existing session
        if existing_session_id:
            claude_cmd.extend(['--resume', existing_session_id])
            logger.info(f"Resuming Claude Code session: {existing_session_id}")

        # Read prompt from the mounted file
        # We use 'cat file | wrapper | claude -' pattern
        # NEW: Use wrapper to enable container-side Redis writes
        wrapper_cmd = [
            'python3', '/app/scripts/docker-claude-wrapper.py',
            '--print', '--verbose', '--output-format', 'stream-json',
            '--model', claude_model,
        ]

        # Add MCP config if provided
        if mcp_config_path:
            wrapper_cmd.extend(['--mcp-config', '/home/orchestrator/.mcp_config.json'])

        # Use bypassPermissions for all agents
        wrapper_cmd.extend(['--permission-mode', 'bypassPermissions'])

        # Add --resume flag if continuing an existing session
        if existing_session_id:
            wrapper_cmd.extend(['--resume', existing_session_id])

        wrapper_cmd.append('-')
        claude_exec_cmd = ' '.join(wrapper_cmd)
        
        # Prepare the shell command
        # 1. Setup gh auth if needed
        # 2. Cat prompt file into claude
        # 3. Redirect output to stdout (captured by docker logs)
        
        # Quote the filename to handle any remaining special characters
        quoted_prompt_path = f"'/workspace/{prompt_filename}'"
        
        github_token = os.environ.get('GITHUB_TOKEN')
        if github_token:
            yaml_content = (
                'version: 1\\n'
                'auth:\\n'
                '  github.com:\\n'
                '    oauth_token: $GITHUB_TOKEN\\n'
                '    git_protocol: https\\n'
            )
            shell_cmd = (
                'mkdir -p ~/.config/gh && '
                f'printf \'{yaml_content}\' > ~/.config/gh/hosts.yml && '
                f'ls -la /workspace >&2 && '
                f'echo "DEBUG: Checking prompt file..." >&2 && '
                f'ls -la {quoted_prompt_path} >&2 && '
                f'echo "DEBUG: Running Claude Code..." >&2 && '
                f'cat {quoted_prompt_path} | {claude_exec_cmd} 2>&1'
            )
        else:
            shell_cmd = (
                f'ls -la /workspace >&2 && '
                f'echo "DEBUG: Checking prompt file..." >&2 && '
                f'ls -la {quoted_prompt_path} >&2 && '
                f'echo "DEBUG: Running Claude Code..." >&2 && '
                f'cat {quoted_prompt_path} | {claude_exec_cmd} 2>&1'
            )

        # Add -d for detached mode
        docker_cmd.insert(2, '-d')
        
        # Remove -i if present (we don't need interactive stdin anymore)
        if '-i' in docker_cmd:
            docker_cmd.remove('-i')

        # Construct final command
        full_cmd = docker_cmd + ['sh', '-c', shell_cmd]

        logger.info(f"Executing detached: docker run -d ...")

        obs = context.get('observability')
        agent = context.get('agent', 'unknown')
        task_id = context.get('task_id', 'unknown')
        project = context.get('project', 'unknown')

        # Initialize timing (always needed, even if obs is None)
        import time
        api_start_time = time.time()

        # Emit events
        if obs:
            obs.emit_claude_call_started(agent, task_id, project, claude_model)
            obs.emit_container_launch_started(agent, task_id, project, container_name, image_name)

        try:
            # Launch detached container
            launch_result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True
            )

            if launch_result.returncode != 0:
                error_msg = f"Failed to launch container: {launch_result.stderr}"
                logger.error(error_msg)
                if obs:
                    obs.emit_container_launch_failed(agent, task_id, project, container_name, error_msg)
                raise Exception(error_msg)

            container_id = launch_result.stdout.strip()
            logger.info(f"Launched detached container: {container_id[:12]}")

            # Emit container launch success
            if obs:
                obs.emit_container_launch_succeeded(agent, task_id, project, container_name, container_id)

            # Track active container in Redis for kill switch
            # Registration happens AFTER docker run succeeds so we have the real container_id
            self._register_active_container(container_name, agent, project, task_id, context, container_id=container_id)

            # Stream logs from the detached container
            # This allows the orchestrator to restart without killing the container
            # On restart, we can re-attach to the logs (logic to be added to recovery service)
            
            log_cmd = ['docker', 'logs', '-f', container_name]
            
            process = subprocess.Popen(
                log_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Collect output
            result_parts = []
            stderr_parts = []
            input_tokens = 0
            output_tokens = 0
            session_id = None
            tools_used_tracking = []  # Track tools used without mutating input context

            # Read both stdout and stderr using threads
            import threading

            def read_stream(stream, is_stderr):
                nonlocal session_id, input_tokens, output_tokens, tools_used_tracking
                for line in iter(stream.readline, ''):
                    if not line:
                        break
                    
                    line = line.strip()
                    if not line:
                        continue

                    # Check for token limits in both stdout and stderr
                    breaker = get_breaker()
                    if breaker:
                        is_limit, reset_time = breaker.detect_session_limit(line)
                        if is_limit:
                            breaker.trip(reset_time)
                            logger.error(f"🔴 TRIPPED BREAKER: Token limit detected in output: {line}")

                    if is_stderr:
                        stderr_parts.append(line + '\n')
                        # Log debug output at DEBUG level, errors at ERROR level
                        if line.startswith('DEBUG:') or line.startswith(('total ', 'drwx', '-rw', 'lrwx')):
                            # Debug output from entrypoint script (ls -la, debug messages)
                            logger.debug(f"Container debug: {line}")
                        elif 'error' in line.lower() or 'exception' in line.lower() or 'failed' in line.lower():
                            # Actual errors
                            logger.error(f"Container stderr: {line}")
                        else:
                            # Other stderr (warnings, info)
                            logger.info(f"Container stderr: {line}")
                        continue

                    # Parse stdout (Claude JSON events)
                    try:
                        event = json.loads(line)
                        event_type = event.get('type', 'unknown')

                        if 'session_id' in event and not session_id:
                            session_id = event['session_id']
                            logger.info(f"Captured Claude Code session_id: {session_id}")

                        if stream_callback:
                            stream_callback(event)

                        if 'usage' in event:
                            input_tokens = event['usage'].get('input_tokens', input_tokens)
                            output_tokens = event['usage'].get('output_tokens', output_tokens)
                            logger.debug(f"Token usage: {input_tokens} input, {output_tokens} output")

                        # Log tool use for visibility in orchestrator logs
                        if event_type == 'tool_use':
                            tool_name = event.get('name', 'unknown')
                            logger.info(f"[Claude] Using tool: {tool_name}")

                            # Track tools used in execution-specific list (don't mutate input context)
                            from monitoring.timestamp_utils import utc_isoformat
                            tools_used_tracking.append({
                                'name': tool_name,
                                'timestamp': utc_isoformat()
                            })

                        # Log tool results
                        if event_type == 'tool_result':
                            tool_use_id = event.get('tool_use_id', 'unknown')
                            is_error = event.get('is_error', False)
                            if is_error:
                                logger.warning(f"[Claude] Tool result (error): {tool_use_id}")
                            else:
                                logger.debug(f"[Claude] Tool result: {tool_use_id}")

                        if event_type in ('error', 'error_detail', 'error_event'):
                            error_msg = event.get('message') or event.get('error') or str(event)
                            logger.error(f"🔴 Captured Claude Code error event: {error_msg}")
                            stderr_parts.append(f"CLAUDE_CODE_ERROR: {error_msg}\n")

                        if event_type == 'assistant':
                            message = event.get('message', {})
                            content = message.get('content', [])
                            for item in content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
                                    if text:
                                        result_parts.append(text)
                                        # Log Claude output to orchestrator logs for visibility
                                        # Truncate long outputs to avoid log spam
                                        log_text = text[:500] + '...' if len(text) > 500 else text
                                        logger.info(f"[Claude] {log_text}")

                    except json.JSONDecodeError:
                        # Non-JSON output might be error messages or raw text
                        # In docker logs, stdout and stderr might be mixed if tty is enabled, 
                        # but we didn't use -t. 
                        # However, docker logs combines streams if we don't separate them carefully.
                        # Here we are reading process.stdout which corresponds to container stdout.
                        logger.warning(f"Non-JSON stdout: {line}")
                        stderr_parts.append(f"STDOUT: {line}\n")

            # Start reader threads
            stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, False), daemon=True)
            stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, True), daemon=True)
            
            stdout_thread.start()
            stderr_thread.start()

            # Wait for container to finish
            # We use 'docker wait' instead of process.wait() because 'docker logs -f' might hang
            # or we might want to stop following if the container dies.

            wait_result = subprocess.run(['docker', 'wait', container_name], capture_output=True, text=True)
            exit_code = int(wait_result.stdout.strip())

            # Allow log streamer to finish naturally
            # 'docker logs -f' should exit when the container stops, but we give it a timeout
            # to ensure we capture the final output (like session limit messages)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning(f"Log streamer for {container_name} timed out, terminating...")
                process.terminate()

            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

            # Log stderr summary for failed containers
            if exit_code != 0:
                stderr_text = ''.join(stderr_parts)
                logger.info(f"Container {container_name} failed with exit_code={exit_code}, stderr length={len(stderr_text)} bytes")

                # Emit container execution failure event
                if obs:
                    duration_ms = (time.time() - api_start_time) * 1000
                    # Extract first error line for event
                    error_preview = stderr_text[:200] if stderr_text else "No error output"
                    obs.emit_container_execution_failed(agent, task_id, project, container_name, exit_code, error_preview, duration_ms)

            # Cleanup ALL prompt files in project directory
            # This handles accumulation from multiple runs and prevents workspace contamination
            try:
                import glob
                project_prompt_files = glob.glob(str(project_dir / '.claude_prompt_*.txt'))
                for prompt_file in project_prompt_files:
                    try:
                        os.remove(prompt_file)
                        logger.debug(f"Cleaned up prompt file: {os.path.basename(prompt_file)}")
                    except Exception as e:
                        logger.warning(f"Failed to remove prompt file {prompt_file}: {e}")
                if project_prompt_files:
                    logger.info(f"Cleaned up {len(project_prompt_files)} prompt file(s) after agent completion")
            except Exception as e:
                logger.warning(f"Failed to cleanup prompt files: {e}")

            # Emit completion
            if obs:
                import time
                api_duration_ms = (time.time() - api_start_time) * 1000
                # Emit container execution completed (with exit code)
                obs.emit_container_execution_completed(agent, task_id, project, container_name, exit_code, api_duration_ms)
                # Emit Claude call completed (success determined by exit_code)
                obs.emit_claude_call_completed(agent, task_id, project, api_duration_ms,
                                               input_tokens, output_tokens, success=(exit_code == 0))

            if exit_code == 0:
                result_text = ''.join(result_parts)

                # CRITICAL: Validate output before marking as success
                # Exit code 0 with empty output is a failure (wrapper couldn't persist, or Claude crashed)
                is_valid, validation_error = self._validate_result(exit_code, result_text, container_name)

                if not is_valid:
                    # Validation failed - treat as failure even though exit code was 0
                    logger.error(
                        f"❌ Container {container_name} validation failed: {validation_error} - "
                        f"marking as failure to trigger retry"
                    )

                    # Persist failure result to Redis
                    if 'issue_number' in context.get('context', {}):
                        task_context = context.get('context', {})
                        issue_number = task_context['issue_number']
                        self._persist_agent_result_to_redis(
                            container_name=container_name,
                            project=project,
                            issue_number=issue_number,
                            agent=agent,
                            task_id=task_id,
                            exit_code=1,  # Mark as failure exit code
                            output=f"VALIDATION FAILED: {validation_error}\n\nOutput:\n{result_text}"
                        )

                        # Record as failure
                        try:
                            from services.work_execution_state import work_execution_tracker
                            column = task_context.get('column', 'unknown')

                            if column != 'unknown':
                                work_execution_tracker.record_execution_outcome(
                                    issue_number=issue_number,
                                    column=column,
                                    agent=agent,
                                    outcome='failure',
                                    project_name=project,
                                    error=validation_error
                                )
                                logger.info(
                                    f"✓ Docker runner recorded validation failure for {project}/#{issue_number} {agent} in {column}"
                                )
                        except Exception as outcome_error:
                            logger.error(f"Failed to record validation failure outcome: {outcome_error}", exc_info=True)

                    # Return empty string to indicate failure
                    return ""

                # Validation passed - proceed with success
                logger.info(f"Agent completed successfully in container, result length: {len(result_text)}")

                # NEW: Persist result to Redis BEFORE processing
                # This ensures result survives if orchestrator crashes during processing
                if 'issue_number' in context.get('context', {}):
                    task_context = context.get('context', {})
                    issue_number = task_context['issue_number']
                    self._persist_agent_result_to_redis(
                        container_name=container_name,
                        project=project,
                        issue_number=issue_number,
                        agent=agent,
                        task_id=task_id,
                        exit_code=exit_code,
                        output=result_text
                    )

                # CRITICAL: Record successful outcome immediately before any result processing
                # This ensures outcome is recorded even if result processing fails
                if 'issue_number' in context.get('context', {}):
                    try:
                        from services.work_execution_state import work_execution_tracker
                        task_context = context.get('context', {})
                        issue_number = task_context['issue_number']
                        column = task_context.get('column', 'unknown')

                        if column != 'unknown':
                            work_execution_tracker.record_execution_outcome(
                                issue_number=issue_number,
                                column=column,
                                agent=agent,
                                outcome='success',
                                project_name=project,
                                error=None
                            )
                            logger.info(
                                f"✓ Docker runner recorded success for {project}/#{issue_number} {agent} in {column}"
                            )
                    except Exception as outcome_error:
                        logger.error(f"Failed to record outcome in docker_runner: {outcome_error}", exc_info=True)

                # Close Claude Code breaker if it was open/half-open
                # This allows recovery after rate limit reset
                breaker = get_breaker()
                if breaker and (breaker.is_open() or breaker.is_half_open()):
                    logger.info("🟢 Agent succeeded - closing Claude Code breaker")
                    breaker.close()

                if session_id:
                    context['claude_session_id'] = session_id

                # Return result with tools_used metadata for validation
                # Use execution-specific tracking (not from input context)
                return {
                    'success': True,
                    'result': result_text,
                    'session_id': session_id,
                    'tools_used': tools_used_tracking
                }
            else:
                stderr_text = ''.join(stderr_parts)

                # NEW: Persist result to Redis even on failure
                # This ensures error output survives if orchestrator crashes
                if 'issue_number' in context.get('context', {}):
                    task_context = context.get('context', {})
                    issue_number = task_context['issue_number']
                    self._persist_agent_result_to_redis(
                        container_name=container_name,
                        project=project,
                        issue_number=issue_number,
                        agent=agent,
                        task_id=task_id,
                        exit_code=exit_code,
                        output=stderr_text
                    )

                # Check if this might be a rate limit error
                # Claude Code exits with code 1 when rate limited, but the "Limit reached" message
                # is not always captured by docker logs (especially in detached mode)
                # Detect this by checking if stderr only contains our debug output (ls, DEBUG messages)
                # and no actual Claude Code error
                breaker = get_breaker()
                if exit_code == 1 and breaker:
                    # Check if stderr only has debug/ls output (no real errors)
                    has_real_error = any(
                        keyword in stderr_text.lower() 
                        for keyword in ['error:', 'exception:', 'traceback', 'failed to', 'cannot', 'invalid']
                    )
                    # Exclude our own debug messages and file listings from the check
                    debug_only = all(
                        line.startswith('STDOUT:') or  # Our debug marker
                        line.startswith('drwx') or line.startswith('-rw') or  # ls output
                        line.startswith('total ') or  # ls summary
                        'DEBUG:' in line or  # Our debug messages
                        line.strip() == '' or  # Empty lines
                        '.gitconfig is a directory' in line  # Entrypoint warning
                        for line in stderr_text.split('\n')
                    )
                    
                    if debug_only and not has_real_error:
                        logger.warning(
                            f"🔴 Container exited with code 1 but stderr contains only debug output. "
                            f"This indicates Claude Code failed without logging (likely rate limit)."
                        )
                        # If breaker is already open, we know it's a rate limit
                        if breaker.state == breaker.OPEN:
                            logger.error("🔴 Claude Code breaker is OPEN - rate limit confirmed")
                            raise Exception("Claude Code rate limit reached. Please wait for reset.")
                        # Otherwise, try to verify if this is actually a rate limit
                        else:
                            logger.warning("🟡 Suspicious exit 1 with no error output - verifying if rate limit...")

                            # Try to detect actual reset time by running a quick test
                            # This captures the "Limit reached · resets 3pm (UTC)" message
                            reset_time = self._detect_rate_limit_reset_time(project_dir)

                            if not reset_time:
                                # Could not verify it's a rate limit - treat as regular failure
                                logger.warning(
                                    "⚠️ Could not confirm rate limit. Exit 1 with no output could be: "
                                    "CLI bug, auth issue, config error, etc. Treating as regular failure."
                                )
                                # Let the normal error handling take over
                                # Don't trip the circuit breaker without confirmation
                            else:
                                # Confirmed rate limit - trip the breaker
                                logger.error(f"🔴 CONFIRMED rate limit. Tripping breaker. Resets at: {reset_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                                breaker.trip(reset_time)
                                raise Exception(f"Claude Code rate limit confirmed. Breaker tripped. Resets at {reset_time.strftime('%I:%M %p')}.")
                
                if not stderr_text:
                    stderr_text = f"Container exited with code {exit_code} but no error output captured."

                # Extract the most useful error info from stderr
                # Skip debug output (ls, echo) and find actual error messages
                error_lines = []
                for line in stderr_text.split('\n'):
                    line_lower = line.lower()
                    # Skip debug output
                    if line.startswith(('total ', 'drwx', '-rw', 'lrwx', 'DEBUG:')):
                        continue
                    # Include lines with error indicators
                    if any(keyword in line_lower for keyword in ['error', 'exception', 'failed', 'traceback', 'errno']):
                        error_lines.append(line)

                # If we found specific errors, use those; otherwise use last 500 chars (most recent output)
                if error_lines:
                    error_summary = '\n'.join(error_lines[:10])  # First 10 error lines
                    stderr_excerpt = error_summary[:500]
                else:
                    # No specific errors found - show end of stderr (most recent output)
                    stderr_excerpt = stderr_text[-500:] if len(stderr_text) > 500 else stderr_text

                logger.error(f"Agent failed in container (exit_code={exit_code}): {stderr_excerpt}")

                # Also log first 500 chars for context (debug level)
                if len(stderr_text) > 500:
                    logger.debug(f"Full stderr (first 500 chars): {stderr_text[:500]}")

                # Emit Claude call failed event
                if obs:
                    api_duration_ms = (time.time() - api_start_time) * 1000
                    obs.emit_claude_call_failed(agent, task_id, project, api_duration_ms, stderr_excerpt, exit_code)

                # CRITICAL: Record failure outcome immediately before raising exception
                # This ensures outcome is recorded even if exception handling fails
                if 'issue_number' in context.get('context', {}):
                    try:
                        from services.work_execution_state import work_execution_tracker
                        task_context = context.get('context', {})
                        issue_number = task_context['issue_number']
                        column = task_context.get('column', 'unknown')

                        if column != 'unknown':
                            work_execution_tracker.record_execution_outcome(
                                issue_number=issue_number,
                                column=column,
                                agent=agent,
                                outcome='failure',
                                project_name=project,
                                error=stderr_excerpt  # Use filtered error instead of truncated stderr
                            )
                            logger.info(
                                f"✓ Docker runner recorded failure for {project}/#{issue_number} {agent} in {column}"
                            )
                    except Exception as outcome_error:
                        logger.error(f"Failed to record outcome in docker_runner: {outcome_error}", exc_info=True)

                self._raise_for_failed_exit_code(exit_code, stderr_excerpt)

        except Exception as e:
            logger.error(f"Agent execution error: {e}")
            # Try to kill container if it's still running
            self._cleanup_container(container_name)

            # Cleanup ALL prompt files in project directory on exception
            try:
                import glob
                project_prompt_files = glob.glob(str(project_dir / '.claude_prompt_*.txt'))
                for prompt_file in project_prompt_files:
                    try:
                        os.remove(prompt_file)
                        logger.debug(f"Cleaned up prompt file after exception: {os.path.basename(prompt_file)}")
                    except Exception as cleanup_err:
                        logger.warning(f"Failed to remove prompt file {prompt_file}: {cleanup_err}")
                if project_prompt_files:
                    logger.info(f"Cleaned up {len(project_prompt_files)} prompt file(s) after exception")
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup prompt files during exception: {cleanup_err}")

            raise

        except subprocess.TimeoutExpired:
            logger.error("Agent execution timed out")
            self._cleanup_container(container_name)

            # Cleanup ALL prompt files in project directory on timeout
            try:
                import glob
                project_prompt_files = glob.glob(str(project_dir / '.claude_prompt_*.txt'))
                for prompt_file in project_prompt_files:
                    try:
                        os.remove(prompt_file)
                        logger.debug(f"Cleaned up prompt file after timeout: {os.path.basename(prompt_file)}")
                    except Exception as cleanup_err:
                        logger.warning(f"Failed to remove prompt file {prompt_file}: {cleanup_err}")
                if project_prompt_files:
                    logger.info(f"Cleaned up {len(project_prompt_files)} prompt file(s) after timeout")
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup prompt files during timeout: {cleanup_err}")

            raise Exception("Agent execution timed out")
        except Exception as e:
            logger.error(f"Agent execution error: {e}")

            # Cleanup ALL prompt files in project directory on exception
            try:
                import glob
                project_prompt_files = glob.glob(str(project_dir / '.claude_prompt_*.txt'))
                for prompt_file in project_prompt_files:
                    try:
                        os.remove(prompt_file)
                        logger.debug(f"Cleaned up prompt file after exception: {os.path.basename(prompt_file)}")
                    except Exception as cleanup_err:
                        logger.warning(f"Failed to remove prompt file {prompt_file}: {cleanup_err}")
                if project_prompt_files:
                    logger.info(f"Cleaned up {len(project_prompt_files)} prompt file(s) after exception")
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup prompt files during exception: {cleanup_err}")

            raise

    def _register_active_container(self, container_name: str, agent: str, project: str, task_id: str, context: Dict[str, Any], container_id: str = None):
        """Register an active container in Redis for tracking and kill switch.

        Retries up to 3 times with backoff to handle transient Redis failures.
        The container_id is passed directly from docker run output.
        """
        import time
        from datetime import datetime

        # Note: agent_executor nests task_context in context['context']
        task_context = context.get('context', {})

        container_info = {
            'container_name': container_name,
            'container_id': container_id or '',
            'agent': agent,
            'project': project,
            'task_id': task_id,
            'started_at': datetime.now().isoformat(),
            # Try nested context first, then top-level (for backwards compatibility)
            'issue_number': str(task_context.get('issue_number') or context.get('issue_number', 'unknown')),
            'pipeline_run_id': task_context.get('pipeline_run_id') or context.get('pipeline_run_id', ''),
            'execution_type': task_context.get('execution_type') or context.get('execution_type', '')
        }

        backoff_times = [0.5, 1.0]
        for attempt in range(3):
            try:
                redis_client = self._get_redis()
                if redis_client is None:
                    raise ConnectionError("Redis unavailable")

                redis_client.hset(f'agent:container:{container_name}', mapping=container_info)
                redis_client.expire(f'agent:container:{container_name}', 7200)

                logger.info(f"Registered active container: {container_name} (agent={agent}, project={project}, id={container_id})")
                return

            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Failed to register container in Redis (attempt {attempt+1}/3): {e}")
                    # Force reconnection on next attempt
                    self._redis = None
                    time.sleep(backoff_times[attempt])
                else:
                    logger.error(
                        f"Container {container_name} is running but UNTRACKED after 3 attempts: {e}. "
                        f"The stuck execution checker will attempt repair."
                    )

    def _unregister_active_container(self, container_name: str):
        """Remove container from active tracking"""
        try:
            redis_client = self._get_redis()
            if redis_client:
                redis_client.delete(f'agent:container:{container_name}')
                logger.info(f"Unregistered container: {container_name}")
        except Exception as e:
            logger.warning(f"Failed to unregister container from Redis: {e}")

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

    def _raise_for_failed_exit_code(self, exit_code: int, stderr_excerpt: str):
        """Raise appropriate exception for a non-zero exit code.

        SIGKILL (137) and SIGTERM (143) raise NonRetryableAgentError since the
        container was deliberately terminated (e.g., user killed via Web UI or
        OOM killer) and retrying will not help.
        """
        if exit_code in (137, 143):
            from agents.non_retryable import NonRetryableAgentError
            raise NonRetryableAgentError(
                f"Agent container was terminated by signal (exit_code={exit_code}): {stderr_excerpt}"
            )
        raise Exception(f"Agent execution failed (exit_code={exit_code}): {stderr_excerpt}")

    def _validate_result(self, exit_code: int, result_text: str, container_name: str) -> tuple:
        """
        Validate that exit code and result match expected success criteria.

        Exit code 0 should mean success, but only if we actually have output.
        Empty output with exit 0 indicates:
        - Wrapper failed to persist result (Redis + file both failed)
        - Claude crashed after wrapper started but before producing output
        - Container was killed mid-execution

        All of these are failures that should trigger retry.

        Returns:
            (is_valid: bool, error_message: str)
            - (True, '') if valid success
            - (False, error_message) if validation failed
        """
        if exit_code == 0:
            # Success exit code - validate we actually have output
            if not result_text or result_text.strip() == '':
                return (
                    False,
                    'Container exited with code 0 but produced no output (wrapper persistence failure or Claude crash)'
                )

            # Additional validation: check if output is just whitespace or minimal
            stripped = result_text.strip()
            if len(stripped) < 50:
                return (
                    False,
                    f'Container produced insufficient output: {len(stripped)} chars (minimum 50 expected)'
                )

            # Check for error markers in output (even though exit code was 0)
            error_markers = [
                'CRITICAL ERROR',
                'FATAL:',
                'Traceback (most recent call last):',
                'redis.exceptions',
                'ConnectionRefusedError'
            ]

            for marker in error_markers:
                if marker in result_text[:1000]:  # Check first 1KB
                    return (
                        False,
                        f'Output contains error marker "{marker}" despite exit code 0'
                    )

            # All validations passed
            return (True, '')

        else:
            # Non-zero exit code is expected failure (no validation needed)
            return (True, '')

    def _persist_agent_result_to_redis(
        self,
        container_name: str,
        project: str,
        issue_number: int,
        agent: str,
        task_id: str,
        exit_code: int,
        output: str
    ):
        """
        Persist agent execution result to Redis for recovery.

        This provides safety during the ~30s orchestrator recovery window.
        If container exits while orchestrator is down, result is not lost.

        Args:
            container_name: Container name
            project: Project name
            issue_number: Issue number
            agent: Agent name
            task_id: Task ID
            exit_code: Container exit code
            output: Agent output (stdout/stderr combined)
        """
        try:
            import json
            from datetime import datetime, timezone

            redis_client = self._get_redis()
            if redis_client is None:
                logger.warning("Failed to persist agent result to Redis: Redis unavailable")
                return

            result = {
                'container_name': container_name,
                'project': project,
                'issue_number': issue_number,
                'agent': agent,
                'task_id': task_id,
                'exit_code': exit_code,
                'output': output,
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'recovered': False
            }

            # Store result with 2-hour TTL (same as container age limit)
            redis_key = f"agent_result:{project}:{issue_number}:{task_id}"
            redis_client.setex(redis_key, 7200, json.dumps(result))

            logger.info(f"Persisted agent result to Redis: {redis_key}")

        except Exception as e:
            logger.warning(f"Failed to persist agent result to Redis: {e}")
            # Don't fail the whole execution if Redis write fails

    def reconnect_to_container(
        self,
        container_name: str,
        project: str,
        issue_number: int,
        agent: str,
        task_id: str,
        column: str = 'unknown'
    ):
        """
        Reconnect to a running container after orchestrator restart.

        Spawns monitoring thread to wait for container exit and process results.
        This is called during container recovery to restore monitoring.

        Args:
            container_name: Container name
            project: Project name
            issue_number: Issue number
            agent: Agent name
            task_id: Task ID
            column: Column name from execution history for proper state matching (default: 'unknown')
        """
        logger.info(f"Reconnecting to container {container_name}")

        # Spawn daemon thread to monitor container until exit
        monitoring_thread = threading.Thread(
            target=self._monitor_recovered_container,
            args=(container_name, project, issue_number, agent, task_id, column),
            daemon=True
        )
        monitoring_thread.start()

        logger.info(f"✓ Monitoring thread started for recovered container {container_name}")

    def _monitor_recovered_container(
        self,
        container_name: str,
        project: str,
        issue_number: int,
        agent: str,
        task_id: str,
        column: str = 'unknown'
    ):
        """
        Monitor a recovered container until it exits.

        This runs in a separate daemon thread and waits for container to exit,
        then processes the results (checks Redis for persisted output, posts to GitHub, etc.)

        Args:
            container_name: Container name
            project: Project name
            issue_number: Issue number
            agent: Agent name
            task_id: Task ID
            column: Column name for proper execution state matching (default: 'unknown')
        """
        try:
            logger.info(
                f"Monitoring recovered container {container_name} "
                f"(project={project}, issue=#{issue_number}, agent={agent})"
            )

            # Wait for container to exit (blocks until container stops)
            result = subprocess.run(
                ['docker', 'wait', container_name],
                capture_output=True,
                text=True,
                timeout=7200  # 2 hours max
            )

            exit_code = int(result.stdout.strip()) if result.stdout.strip() else -1

            logger.info(
                f"Recovered container {container_name} exited with code {exit_code}"
            )

            # Try to get persisted result from Redis
            output = None
            redis_key = f"agent_result:{project}:{issue_number}:{task_id}"
            logger.info(f"Attempting to retrieve result from Redis: {redis_key}")
            try:
                import redis
                import json
                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

                result_json = redis_client.get(redis_key)

                if result_json:
                    result_data = json.loads(result_json)
                    output = result_data.get('output', '')
                    logger.info(f"✓ Loaded persisted result from Redis: {redis_key}")

                    # Mark as recovered and update TTL
                    result_data['recovered'] = True
                    redis_client.setex(redis_key, 300, json.dumps(result_data))  # 5 min TTL
                else:
                    logger.warning(
                        f"No persisted result in Redis for {redis_key} - "
                        f"container may have exited before result was written, will try fallback methods"
                    )
            except Exception as e:
                logger.error(f"Failed to load persisted result from Redis: {e}")

            # Fallback 2: Try to get result from container's fallback file (/tmp/agent_result_{task_id}.json)
            # This is written by docker-claude-wrapper.py when Redis write fails
            if not output:
                logger.info(f"Attempting fallback retrieval via docker cp from {container_name}")
                try:
                    result_file = f"/tmp/agent_result_{task_id}.json"
                    result = subprocess.run(
                        ['docker', 'cp', f'{container_name}:{result_file}', '-'],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if result.returncode == 0 and result.stdout:
                        import json
                        fallback_data = json.loads(result.stdout)
                        output = fallback_data.get('output', '')
                        logger.info(
                            f"✓ Retrieved result from container fallback file: {result_file} "
                            f"(Redis write failed during execution)"
                        )
                    else:
                        logger.info(
                            f"No fallback file found in container at {result_file} "
                            f"(returncode={result.returncode}, stderr={result.stderr[:200] if result.stderr else 'none'})"
                        )
                except Exception as e:
                    logger.warning(f"Could not retrieve fallback file from container: {e}")

            # Fallback 3: Try to get logs from stopped container (if still exists)
            if not output:
                logger.info(f"Attempting fallback retrieval via docker logs from {container_name}")
                try:
                    result = subprocess.run(
                        ['docker', 'logs', container_name],
                        capture_output=True,
                        text=True,
                        timeout=60
                    )
                    if result.returncode == 0:
                        output = result.stdout + result.stderr
                        logger.info(f"✓ Retrieved logs from stopped container {container_name}")
                    else:
                        logger.info(
                            f"Could not retrieve logs from container {container_name} "
                            f"(returncode={result.returncode}, stderr={result.stderr[:200] if result.stderr else 'none'})"
                        )
                except Exception as e:
                    logger.warning(f"Could not retrieve logs from container {container_name}: {e}")

            # Process the completion (post to GitHub, record execution outcome, etc.)
            if output:
                self._process_recovered_container_completion(
                    container_name=container_name,
                    project=project,
                    issue_number=issue_number,
                    agent=agent,
                    task_id=task_id,
                    exit_code=exit_code,
                    output=output,
                    column=column
                )
            else:
                logger.error(
                    f"No output available for recovered container {container_name} - "
                    f"cannot process results"
                )
                # Mark execution as failed
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.record_execution_outcome(
                    issue_number=issue_number,
                    column=column,  # Use actual column instead of 'unknown'
                    agent=agent,
                    outcome='failed',
                    project_name=project,
                    error='Container completed after restart but no output was available'
                )

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout waiting for recovered container {container_name}")
        except Exception as e:
            logger.error(f"Error monitoring recovered container {container_name}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _process_recovered_container_completion(
        self,
        container_name: str,
        project: str,
        issue_number: int,
        agent: str,
        task_id: str,
        exit_code: int,
        output: str,
        column: str = 'unknown'
    ):
        """
        Process completion of a recovered container.

        Posts output to GitHub, records execution outcome, cleans up container.
        Similar to normal completion flow but handles the recovery case.

        Args:
            container_name: Container name
            project: Project name
            issue_number: Issue number
            agent: Agent name
            task_id: Task ID
            exit_code: Container exit code
            output: Agent output
            column: Column name for proper execution state matching (default: 'unknown')
        """
        try:
            logger.info(
                f"Processing recovered container completion: {container_name} "
                f"(exit_code={exit_code})"
            )

            # Get GitHub integration
            from services.github_integration import GitHubIntegration
            from config.manager import config_manager

            project_config = config_manager.get_project_config(project)
            github = GitHubIntegration(
                repo_owner=project_config.github['org'],
                repo_name=project_config.github['repo']
            )

            # Post output to GitHub (same as normal flow)
            import asyncio
            context = {
                'issue_number': issue_number,
                'repository': project_config.github.get('repo', project),
                'workspace_type': 'issues'
            }

            # Post output using asyncio.run()
            asyncio.run(
                github.post_agent_output(context, output)
            )

            logger.info(f"Posted recovered container output to GitHub issue #{issue_number}")

            # Record execution outcome
            from services.work_execution_state import work_execution_tracker

            outcome = 'success' if exit_code == 0 else 'failed'
            error = None if exit_code == 0 else f"Container exited with code {exit_code}"

            work_execution_tracker.record_execution_outcome(
                issue_number=issue_number,
                column=column,  # Use actual column from execution history
                agent=agent,
                outcome=outcome,
                project_name=project,
                error=error
            )

            logger.info(
                f"Recorded execution outcome for recovered container: "
                f"{project}/#{issue_number} {agent} → {outcome}"
            )

            # Auto-advance to next column if successful and column has auto_advance_on_approval
            if exit_code == 0 and column != 'unknown':
                try:
                    # Find the pipeline and workflow for this column
                    for pipeline in project_config.pipelines:
                        workflow_template = config_manager.get_workflow_template(pipeline.workflow)
                        if not workflow_template:
                            continue

                        current_column = next(
                            (c for c in workflow_template.columns if c.name == column),
                            None
                        )

                        if current_column and getattr(current_column, 'type', None) == 'review':
                            logger.info(
                                f"Skipping auto-advance for recovered container: "
                                f"column '{column}' is a review cycle column. "
                                f"Review cycle will handle progression."
                            )
                            break  # Skip auto-advance, review cycle owns progression

                        if current_column and getattr(current_column, 'auto_advance_on_approval', False):
                            current_index = workflow_template.columns.index(current_column)
                            if current_index + 1 < len(workflow_template.columns):
                                next_column = workflow_template.columns[current_index + 1]

                                logger.info(
                                    f"Auto-advancing recovered container issue #{issue_number} "
                                    f"from {column} to {next_column.name}"
                                )

                                from services.pipeline_progression import PipelineProgression
                                from task_queue.task_manager import TaskQueue

                                task_queue = TaskQueue()
                                progression_service = PipelineProgression(task_queue)

                                moved = progression_service.move_issue_to_column(
                                    project_name=project,
                                    board_name=pipeline.board_name,
                                    issue_number=issue_number,
                                    target_column=next_column.name,
                                    trigger='recovered_container_auto_advance'
                                )

                                if moved:
                                    logger.info(
                                        f"Successfully auto-advanced issue #{issue_number} to {next_column.name}"
                                    )
                                else:
                                    logger.warning(
                                        f"Failed to auto-advance issue #{issue_number} to {next_column.name}"
                                    )
                            break  # Found the column's pipeline, stop searching
                except Exception as e:
                    logger.error(f"Error during recovered container auto-advancement: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

            # Clean up container tracking
            self._unregister_active_container(container_name)

            # Try to remove container (may already be removed by --rm)
            try:
                subprocess.run(['docker', 'rm', '-f', container_name], timeout=30)
            except Exception:
                pass  # Container may already be auto-removed

            logger.info(f"✓ Completed processing recovered container {container_name}")

        except Exception as e:
            logger.error(f"Error processing recovered container completion: {e}")
            import traceback
            logger.error(traceback.format_exc())

    @staticmethod
    def cleanup_orphaned_redis_keys():
        """
        Clean up Redis tracking keys for containers that no longer exist.

        This is called on orchestrator startup to handle cases where:
        - Orchestrator was restarted while agents were running
        - Containers finished after orchestrator restart
        - Redis keys were never cleaned up because finally blocks didn't execute
        """
        try:
            import redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

            # Get all agent container tracking keys
            agent_keys = redis_client.keys('agent:container:*')

            if not agent_keys:
                logger.info("No agent container tracking keys found in Redis")
                return

            logger.info(f"Checking {len(agent_keys)} agent container tracking keys for orphans")

            cleaned_count = 0
            for key in agent_keys:
                try:
                    # Get container name from Redis
                    container_info = redis_client.hgetall(key)
                    container_name = container_info.get('container_name')

                    if not container_name:
                        # Invalid tracking key, remove it
                        redis_client.delete(key)
                        cleaned_count += 1
                        logger.info(f"Removed invalid tracking key: {key}")
                        continue

                    # Check if container exists
                    result = subprocess.run(
                        ['docker', 'ps', '-a', '--filter', f'name={container_name}', '--format', '{{.Names}}'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )

                    if result.returncode == 0:
                        # Parse output to see if container exists
                        existing_containers = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]

                        if container_name not in existing_containers:
                            # Container doesn't exist, remove tracking key
                            redis_client.delete(key)
                            cleaned_count += 1
                            logger.info(f"Cleaned up orphaned tracking key for non-existent container: {container_name}")
                        else:
                            logger.debug(f"Container {container_name} still exists, keeping tracking key")

                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout checking container existence for {key}")
                except Exception as e:
                    logger.warning(f"Error checking container {key}: {e}")

            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} orphaned container tracking keys")
            else:
                logger.info("No orphaned container tracking keys found")

        except Exception as e:
            logger.error(f"Error during orphaned Redis key cleanup: {e}")


# Global instance
docker_runner = DockerAgentRunner()
