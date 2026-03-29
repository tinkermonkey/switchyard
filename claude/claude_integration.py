import subprocess
import json
import os
import logging
import threading
import time
from typing import Dict, Any
from pathlib import Path
from claude.docker_runner import docker_runner
from services.project_workspace import workspace_manager

try:
    import redis as redis_lib
except ImportError:
    redis_lib = None

logger = logging.getLogger(__name__)

async def run_claude_code(prompt: str, context: Dict[str, Any]) -> str:
    """Execute Claude Code with given prompt and context"""
    logger.info("run_claude_code called")
    logger.info(f"Context project: {context.get('project', 'unknown')}")
    logger.info(f"Prompt length: {len(prompt)}")

    # Get observability manager from context
    obs = context.get('observability')
    task_id = context.get('task_id', 'unknown')
    agent = context.get('agent', 'unknown')
    project = context.get('project', 'unknown')
    
    # CRITICAL: Log if project is unknown - this should NEVER happen in production
    if project == 'unknown':
        logger.error(f"CRITICAL: project='unknown' in claude_integration context!")
        logger.error(f"Context keys: {list(context.keys())}")
        logger.error(f"Agent: {agent}, Task ID: {task_id}")

    # Emit prompt constructed event with rough component size breakdown
    if obs:
        task_context = context.get('context', {})
        task_description = task_context.get('task_description', '') or task_context.get('requirements', '')
        task_chars = len(str(task_description))
        context_chars = len(str(task_context)) - task_chars
        system_prompt_chars = max(0, len(prompt) - task_chars - max(0, context_chars))
        pipeline_run_id = task_context.get('pipeline_run_id') or context.get('pipeline_run_id')
        obs.emit_prompt_constructed(agent, task_id, project, prompt,
                                    prompt_components={
                                        'system_prompt_chars': system_prompt_chars,
                                        'context_chars': max(0, context_chars),
                                        'task_chars': task_chars
                                    },
                                    pipeline_run_id=pipeline_run_id)

    # Get MCP server configuration from context
    mcp_servers = context.get('mcp_servers', [])

    # Get agent configuration to determine if Docker is required
    agent_config = context.get('agent_config')

    # DEBUG: Log decision-making process
    logger.info(f"Agent {agent}: use_docker from context={context.get('use_docker')}, agent_config present={agent_config is not None}")
    if agent_config:
        logger.info(f"Agent {agent}: agent_config type={type(agent_config)}, has requires_docker={hasattr(agent_config, 'requires_docker')}")

    # CRITICAL: Agent's requires_docker setting takes precedence over context
    # Only dev_environment_setup should have requires_docker=False
    if agent_config and hasattr(agent_config, 'requires_docker'):
        use_docker = agent_config.requires_docker
        logger.info(f"Agent {agent}: Using agent_config.requires_docker={use_docker}")
        if not use_docker:
            logger.warning(f"Agent {agent} is configured to run LOCALLY (requires_docker=False) - this should ONLY be dev_environment_setup!")
    else:
        # Fallback to context, but default to True for security
        use_docker = context.get('use_docker', True)
        logger.warning(f"Agent {agent}: No agent_config.requires_docker, using context value: {use_docker}")

    if use_docker:
        if project == 'unknown':
            logger.error(f"CRITICAL: Agent {agent} requires Docker but project='unknown'")
            logger.error(f"Task ID: {task_id}")
            logger.error(f"Context keys present: {list(context.keys())}")
            logger.error(f"This indicates a bug in the pipeline - context should always have 'project'")
            raise Exception(
                f"Agent {agent} requires Docker but project is unknown - cannot determine project directory. "
                f"Context keys: {list(context.keys())}"
            )
        
        logger.info(f"Running agent in Docker container for project {project}")

        # Get project directory from workspace manager
        project_dir = workspace_manager.get_project_dir(project)

        if not project_dir.exists():
            raise Exception(f"Project directory does not exist: {project_dir}")

        # Run in Docker container - if this fails, we MUST fail, not fall back
        return await docker_runner.run_agent_in_container(
            prompt=prompt,
            context=context,
            project_dir=project_dir,
            mcp_servers=mcp_servers,
            stream_callback=context.get('stream_callback')
        )

    # Only reach here if use_docker=False (dev_environment_setup and dev_environment_verifier only)
    logger.warning(f"Running agent {agent} locally (not in Docker) - this should ONLY be dev_environment_setup or dev_environment_verifier!")

    # Prepare working directory
    work_dir = Path(context.get('work_dir', '.'))
    logger.info(f"Work directory: {work_dir}")

    # Prepare context information
    context_info = f"""
Project: {context.get('project', 'unknown')}
Task: {context.get('task_description', '')}
Files: {context.get('files', [])}
"""

    # For now, simulate Claude Code execution since we may not have it installed
    # In production, this would execute: claude -p "prompt" --output-format json

    try:
        # Check if claude command is available
        logger.info("Checking if claude CLI is available")
        result = subprocess.run(['which', 'claude'], capture_output=True, text=True, timeout=5)
        logger.info(f"which claude returned: {result.returncode}, output: {result.stdout.strip()}")

        if result.returncode == 0:
            # Claude CLI is available - use it
            logger.info("Claude CLI is available, preparing command")

            # Get configured model from context or use default
            claude_model = context.get('claude_model', 'claude-sonnet-4-5-20250929')
            logger.info(f"Using Claude model: {claude_model}")

            # Check for existing session to resume
            existing_session_id = context.get('claude_session_id')

            cmd = [
                'claude',
                '--print',
                '--verbose',
                '--output-format', 'stream-json',
                '--model', claude_model,
                '--permission-mode', 'bypassPermissions'
            ]

            # Add --resume flag if continuing an existing session
            if existing_session_id:
                cmd.extend(['--resume', existing_session_id])
                logger.info(f"Resuming Claude Code session: {existing_session_id}")

            # NOTE: Do NOT append prompt to cmd - we'll pass it via stdin to avoid ARG_MAX issues
            # Large prompts cause "[Errno 7] Argument list too long" errors
            # cmd.append(prompt)  # REMOVED

            # Ensure working directory exists or use current directory
            if not work_dir.exists():
                logger.info(f"Creating work directory: {work_dir}")
                work_dir.mkdir(parents=True, exist_ok=True)

            # Create .mcp.json file in working directory with MCP server configurations
            if mcp_servers and len(mcp_servers) > 0:
                logger.info(f"Configuring {len(mcp_servers)} MCP servers")
                mcp_config_data = {"mcpServers": {}}

                for server in mcp_servers:
                    server_name = server['name']
                    server_type = server.get('type', 'http')

                    if server_type == 'http':
                        # HTTP-based MCP server
                        url = server['url']
                        logger.info(f"Adding HTTP MCP server: {server_name} at {url}")
                        mcp_config_data["mcpServers"][server_name] = {
                            "type": "http",
                            "url": url
                        }
                    elif server_type == 'stdio':
                        # stdio-based MCP server (like Puppeteer, Serena)
                        command = server['command']
                        args = server.get('args', [])
                        server_env = server.get('env', {})

                        # Substitute {work_dir} template in args with actual work directory
                        substituted_args = []
                        for arg in args:
                            if isinstance(arg, str) and '{work_dir}' in arg:
                                arg = arg.replace('{work_dir}', str(work_dir.absolute()))
                            substituted_args.append(arg)

                        logger.info(f"Adding stdio MCP server: {server_name} with command: {command}")
                        mcp_config_data["mcpServers"][server_name] = {
                            "command": command,
                            "args": substituted_args
                        }
                        if server_env:
                            mcp_config_data["mcpServers"][server_name]["env"] = server_env

                # Write .mcp.json to working directory
                mcp_config_path = work_dir / '.mcp.json'
                with open(mcp_config_path, 'w') as f:
                    json.dump(mcp_config_data, f, indent=2)
                logger.info(f"Created .mcp.json at {mcp_config_path}")
            else:
                logger.info("No MCP servers configured for this agent")

            # Build environment via the shared builder so auth, identification, and OTEL
            # telemetry vars are injected consistently with the containerised launch path.
            # The orchestrator process already runs inside Docker on orchestrator-net, so
            # 'otel-collector' resolves correctly here too. See claude/environment.py.
            from claude.environment import ClaudeEnvironmentBuilder, ClaudeRunContext

            run_ctx = ClaudeRunContext(
                agent_name=agent,
                task_id=task_id,
                project=project,
                issue_number=context.get('issue_number'),
                pipeline_run_id=(
                    context.get('pipeline_run_id')
                    or context.get('context', {}).get('pipeline_run_id')
                ),
            )
            env_builder = ClaudeEnvironmentBuilder(
                otel_host=os.environ.get('OTEL_COLLECTOR_HOST', 'otel-collector')
            )
            # Inherit the full orchestrator environment then overlay Claude-specific vars
            env = {**os.environ.copy(), **env_builder.build(run_ctx)}

            # Connect to Redis for claude-streams-* persistence (fire-and-forget)
            _redis_client = None
            if redis_lib is not None:
                try:
                    _redis_client = redis_lib.Redis(
                        host=env.get('REDIS_HOST', 'redis'),
                        port=int(env.get('REDIS_PORT', 6379)),
                        socket_timeout=1.0,
                        socket_connect_timeout=2.0,
                        decode_responses=False,
                    )
                    _redis_client.ping()
                except Exception:
                    _redis_client = None

            # Resolve pipeline_run_id once for use in Redis stream writes below
            _local_pipeline_run_id = run_ctx.pipeline_run_id or ''
            _local_issue_number = run_ctx.issue_number or ''

            logger.debug(f"Executing Claude CLI in {work_dir}")
            logger.debug(f"Command: {' '.join(cmd[:3])}...")  # Don't log full prompt

            # Emit Claude API call started event
            api_start_time = time.time()
            if obs:
                obs.emit_claude_call_started(agent, task_id, project, claude_model,
                                             pipeline_run_id=_local_pipeline_run_id or None)

            # Get stream callback if websocket is connected
            stream_callback = context.get('stream_callback')

            # Use Popen to stream output in real-time
            # Pass prompt via stdin to avoid ARG_MAX "Argument list too long" errors
            process = subprocess.Popen(
                cmd,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,  # Changed from DEVNULL to PIPE for prompt input
                text=True,
                env=env
            )

            # Write prompt to stdin and close it immediately
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                logger.debug(f"Wrote {len(prompt)} characters to stdin")
            except Exception as e:
                logger.error(f"Failed to write prompt to stdin: {e}")
                process.kill()
                raise Exception(f"Failed to pass prompt to Claude CLI: {e}")

            # Collect all output for final result
            result_parts = []
            input_tokens = 0
            output_tokens = 0
            cache_read_tokens = 0
            cache_creation_tokens = 0
            session_id = None  # Track session ID for continuity
            stderr_lines = []  # Collect stderr for error reporting
            stdout_raw_lines = []  # Collect all stdout for debugging
            error_events = []  # Collect error events from stream

            # Read stderr in a separate thread to prevent blocking
            def read_stderr():
                try:
                    for line in iter(process.stderr.readline, ''):
                        if not line:
                            break
                        stderr_lines.append(line.strip())
                        if line.strip():
                            logger.warning(f"Claude CLI stderr: {line.strip()}")
                except Exception as e:
                    logger.error(f"Error reading stderr: {e}")

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()

            # Stream output line by line
            try:
                logger.info(f"Starting to stream Claude output, callback present: {stream_callback is not None}")
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break

                    line = line.strip()
                    if not line:
                        continue

                    # Always capture raw stdout for debugging
                    stdout_raw_lines.append(line)

                    try:
                        event = json.loads(line)
                        event_type = event.get('type', 'unknown')

                        # Log event structure for debugging
                        if event_type not in ['progress', 'status']:
                            logger.debug(f"Stream event type: {event_type}, keys: {list(event.keys())}")

                        # Capture error events
                        if event_type == 'error':
                            error_events.append(event)
                            error_msg = event.get('error', event.get('message', 'Unknown error'))
                            logger.error(f"Claude CLI error event: {error_msg}")

                        # Capture session_id for session continuity
                        if 'session_id' in event and not session_id:
                            session_id = event['session_id']
                            logger.info(f"Captured Claude Code session_id: {session_id}")

                        # Stream event to websocket if callback provided
                        if stream_callback:
                            stream_callback(event)

                        # Persist event to claude-streams-* via Redis Stream (same path as
                        # docker-claude-wrapper.py for containerized agents)
                        if _redis_client is not None:
                            try:
                                event_envelope = {
                                    'agent': agent,
                                    'task_id': task_id,
                                    'project': project,
                                    'issue_number': _local_issue_number,
                                    'pipeline_run_id': _local_pipeline_run_id,
                                    'timestamp': event.get('timestamp', time.time()),
                                    'event': event,
                                }
                                serialized = json.dumps(event_envelope).encode()
                                _redis_client.xadd(
                                    'orchestrator:claude_logs_stream',
                                    {'log': serialized},
                                    maxlen=50000,
                                    approximate=True,
                                )
                                _redis_client.publish('orchestrator:claude_stream', serialized)
                            except Exception:
                                pass  # Non-blocking — output collection must not block execution

                        # Track token usage from stream events
                        if 'usage' in event:
                            input_tokens = event['usage'].get('input_tokens', input_tokens)
                            output_tokens = event['usage'].get('output_tokens', output_tokens)
                            cache_read_tokens = event['usage'].get('cache_read_input_tokens', cache_read_tokens)
                            cache_creation_tokens = event['usage'].get('cache_creation_input_tokens', cache_creation_tokens)

                        # Capture only the final assistant message — replace on each event so
                        # intermediate reasoning turns don't leak into the posted output.
                        if event_type == 'assistant':
                            message = event.get('message', {})
                            content = message.get('content', [])
                            turn_parts = []
                            for item in content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
                                    if text:
                                        turn_parts.append(text)
                            if turn_parts:
                                result_parts.clear()
                                result_parts.extend(turn_parts)
                                logger.debug(f"Captured assistant turn, length: {sum(len(p) for p in result_parts)}")

                    except json.JSONDecodeError:
                        # Non-JSON output, just log it
                        logger.warning(f"Non-JSON stdout line: {line[:200]}")

                # Wait for process to complete
                process.wait(timeout=600)

                # Wait for stderr thread to finish (with timeout)
                stderr_thread.join(timeout=5)

                api_duration_ms = (time.time() - api_start_time) * 1000

                # Check success before emitting completion event
                success = process.returncode == 0

                # Emit completion event with accurate success flag
                if obs:
                    obs.emit_claude_call_completed(agent, task_id, project, api_duration_ms,
                                                   input_tokens, output_tokens,
                                                   cache_read_tokens=cache_read_tokens,
                                                   cache_creation_tokens=cache_creation_tokens,
                                                   success=success,
                                                   pipeline_run_id=_local_pipeline_run_id or None)

                if process.returncode == 0:
                    result_text = ''.join(result_parts)
                    logger.info(f"Claude CLI completed successfully, result length: {len(result_text)}, session_id: {session_id}")

                    # Store session_id in context for session continuity
                    if session_id:
                        context['claude_session_id'] = session_id

                    # Return just the result text (callers expect string, not dict)
                    return result_text
                else:
                    # Build comprehensive error message
                    error_parts = [f"Claude CLI failed with exit code {process.returncode}"]

                    if stderr_lines:
                        error_parts.append(f"STDERR: {' '.join(stderr_lines)}")

                    if error_events:
                        error_msgs = [e.get('error', e.get('message', str(e))) for e in error_events]
                        error_parts.append(f"ERROR EVENTS: {' | '.join(error_msgs)}")

                    # Log last few stdout lines for context
                    if stdout_raw_lines:
                        last_lines = stdout_raw_lines[-10:]
                        logger.error(f"Last {len(last_lines)} stdout lines:")
                        for i, line in enumerate(last_lines, 1):
                            logger.error(f"  {i}: {line[:500]}")

                    error_message = ' | '.join(error_parts)
                    logger.error(error_message)
                    raise Exception(error_message)

            except subprocess.TimeoutExpired:
                process.kill()
                raise Exception("Claude Code execution timed out")
        else:
            # Claude CLI not available - simulate response for development
            logger.info("Claude CLI not found, simulating response for development")

            # Generate a realistic simulation based on the prompt
            if "business analyst" in prompt.lower() or "requirements" in prompt.lower():
                simulation = {
                    "requirements_analysis": {
                        "summary": "Analyzed requirements from the provided issue",
                        "functional_requirements": [
                            "User authentication system",
                            "Secure login/logout functionality",
                            "Password validation"
                        ],
                        "non_functional_requirements": [
                            "Response time < 2 seconds",
                            "99.9% uptime requirement"
                        ],
                        "user_stories": [
                            {
                                "title": "User Login",
                                "description": "As a user I want to log into the system so that I can access my account",
                                "acceptance_criteria": [
                                    "Given valid credentials when I submit login form then I am authenticated",
                                    "Given invalid credentials when I submit login form then I see error message"
                                ],
                                "priority": "High"
                            }
                        ],
                        "risks": ["Security vulnerabilities", "Performance under load"],
                        "assumptions": ["Users have valid email addresses", "Password complexity requirements agreed"]
                    },
                    "quality_metrics": {
                        "completeness_score": 0.85,
                        "clarity_score": 0.90,
                        "testability_score": 0.80
                    }
                }
                return json.dumps(simulation)
            else:
                # Generic response for other agent types
                return json.dumps({
                    "result": "Task completed successfully",
                    "output": f"Processed prompt: {prompt[:100]}...",
                    "context": context.get('project', 'unknown')
                })

    except subprocess.TimeoutExpired:
        logger.error("Claude Code execution timed out")
        raise Exception("Claude Code execution timed out")
    except Exception as e:
        logger.error(f"Claude Code integration error: {str(e)}", exc_info=True)
        raise Exception(f"Claude Code integration error: {str(e)}")