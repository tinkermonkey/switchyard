import subprocess
import json
import os
import logging
import threading
import time
from typing import Dict, Any
from pathlib import Path
from claude.docker_runner import docker_runner, resolve_final_output
from services.project_workspace import workspace_manager
from services.cancellation import CancellationError
from monitoring.claude_code_breaker import ClaudeCodeRateLimitError

try:
    import redis as redis_lib
except ImportError:
    redis_lib = None

logger = logging.getLogger(__name__)


def _require_work_dir(context: Dict[str, Any], agent: str) -> Path:
    """
    Read the local-execution working directory off `context`, refusing a missing
    one instead of defaulting it (#151/WI-6 item 12).

    Both readers of this value used to be `Path(context.get('work_dir', '.'))`.
    That default is what kept is_base_clone_dir()'s fail-closed branch from ever
    firing for the very case its docstring cites: that branch treats a directory
    that doesn't exist as the shared base clone rather than silently skipping the
    lock, but '.' resolves to the orchestrator's OWN cwd, which always exists --
    so a context with no work_dir fell through to the real comparison, came back
    False, and skipped the project_checkout lock entirely. The fix belongs here
    rather than in is_base_clone_dir(), which cannot tell a deliberate '.' from a
    defaulted one.

    The same default was independently wrong for _run_claude_code_locally(), which
    would have run the agent's Claude Code session in the orchestrator's own
    checkout. Neither is a state to guess at: every production producer of this
    context sets work_dir (agent_executor.py's _build_execution_context, the
    services/workspace/ context classes, services/pipeline_run_analysis.py, and
    the scripts/ entry points), so its absence is a construction bug upstream.
    """
    raw_work_dir = context.get('work_dir')
    if raw_work_dir is None or not str(raw_work_dir).strip():
        raise Exception(
            f"Agent {agent} reached local (non-Docker) execution with no 'work_dir' in "
            f"its context -- there is no directory to run it in, and no basis to decide "
            f"whether the project_checkout lock is needed. This is a bug in whatever "
            f"built this context. Context keys: {list(context.keys())}"
        )
    return Path(str(raw_work_dir))

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

        # Get project directory from workspace manager.
        task_context_for_dir = context.get('context', {}) or {}
        existing_project_dir = task_context_for_dir.get('project_dir')
        if existing_project_dir:
            # A caller running in its own separate process (e.g. a repair cycle
            # container) has already resolved the concrete directory its
            # originating orchestrator process created/reused -- reuse it as-is.
            # Re-deriving via epic_id here would run against a *fresh*, empty
            # in-process worktree cache (ProjectWorkspaceManager's cache is
            # process-local) and could attempt to `git worktree add` a worktree
            # that already exists on disk, which is not idempotent and would raise.
            project_dir = Path(existing_project_dir)
        else:
            # If an earlier stage of this same dispatch
            # (agent_executor._build_execution_context) already resolved the epic
            # this task belongs to, reuse it here so the container mounts the
            # epic's isolated worktree -- not the shared base clone -- instead of
            # re-resolving it a second time. branch_name is only required the
            # first time an epic's worktree is created; by the time this runs it
            # has already been created upstream (or never needed a branch at
            # all), so it is safe to pass along whatever (if anything) is
            # already known.
            epic_id = task_context_for_dir.get('epic_id')
            branch_name = task_context_for_dir.get('branch_name')
            project_dir = workspace_manager.get_project_dir(
                project, epic_id=epic_id, branch_name=branch_name
            )

        if not project_dir.exists():
            raise Exception(f"Project directory does not exist: {project_dir}")

        # Run in Docker container - if this fails, we MUST fail, not fall back
        #
        # project_checkout lock (#54): the container bind-mounts project_dir for
        # its whole run, so if project_dir is the shared base clone (not an
        # isolated epic worktree -- see is_base_clone_dir()) this run must
        # serialize against every other operation touching that same base clone
        # (another board's container run, the startup clone/update, etc.) rather
        # than race it. Epic-worktree-scoped runs are deliberately NOT locked
        # here -- they don't share a directory with anything else, and locking
        # them too would serialize sibling epics for no reason.
        if workspace_manager.is_base_clone_dir(project, project_dir):
            from services.project_checkout_lock import project_checkout_lock_async

            # issue_number here is log attribution only, not the lock's holder
            # identity (every acquisition mints its own -- see
            # project_checkout_lock.py's module docstring), so it's fine for
            # this to be None when no real issue is in scope.
            issue_number_for_lock = task_context_for_dir.get('issue_number') or context.get('issue_number')
            async with project_checkout_lock_async(project, issue_number_for_lock):
                return await docker_runner.run_agent_in_container(
                    prompt=prompt,
                    context=context,
                    project_dir=project_dir,
                    mcp_servers=mcp_servers,
                    stream_callback=context.get('stream_callback')
                )

        return await docker_runner.run_agent_in_container(
            prompt=prompt,
            context=context,
            project_dir=project_dir,
            mcp_servers=mcp_servers,
            stream_callback=context.get('stream_callback')
        )

    # Only reach here if use_docker=False (dev_environment_setup and dev_environment_verifier only)
    #
    # dev_container_build lock (#56): for dev_environment_setup/verifier,
    # this local execution IS this project's dev-container build/verify
    # session -- the Claude Code subprocess started below issues the actual
    # `docker build`/`docker inspect` calls itself, via its own Bash tool,
    # against the orchestrator's own docker socket. There is no other
    # orchestrator-side hook around that work (see
    # services/dev_container_build_lock.py's module docstring for the full
    # investigation), so this call is where the lock is acquired --
    # unconditionally for every agent that reaches this branch, gated only
    # on the `use_docker` flag, not agent identity. A third agent,
    # pipeline_analysis, also has requires_docker: false and reaches here
    # too (found in PR #138 review, /pr-review-toolkit:review-pr -- see
    # #140), acquiring this same lock even though it never builds/inspects
    # anything -- a known, tracked gap, not a correctness issue for
    # dev_environment_setup/verifier themselves.
    task_context_for_dev_lock = context.get('context', {}) or {}
    issue_number_for_dev_lock = task_context_for_dev_lock.get('issue_number') or context.get('issue_number')

    # Resolved BEFORE either lock below (#151/WI-6 item 12): a context with no
    # work_dir is a construction bug in whatever built it, and failing on it
    # here means no lock is taken only to be abandoned a line later. See
    # _require_work_dir() for why this must not fall back to '.'.
    work_dir_for_lock = _require_work_dir(context, agent)

    from services.dev_container_build_lock import dev_container_build_lock_async

    async with dev_container_build_lock_async(project, issue_number_for_dev_lock):
        # project_checkout lock (#54): dev_environment_setup/verifier's cwd is
        # normally an isolated epic worktree (its own issue number, resolved
        # unconditionally for 'issues'/'hybrid' workspace types -- see
        # agent_executor.py's epic-resolution block) and does NOT need this lock.
        # Only lock when work_dir genuinely IS the shared base clone (e.g. a
        # workspace-resolution fallback) -- see is_base_clone_dir()'s docstring
        # for why locking epic-worktree-scoped runs too would be wrong. Distinct
        # resource from dev_container_build above, so nesting the two here is
        # safe -- neither lock is ever acquired twice for the same resource.
        if workspace_manager.is_base_clone_dir(project, work_dir_for_lock):
            from services.project_checkout_lock import project_checkout_lock_async

            # issue_number here is log attribution only -- see the comment at the
            # Docker-branch call site above.
            async with project_checkout_lock_async(project, issue_number_for_dev_lock):
                return await _run_claude_code_locally(prompt, context, agent)

        return await _run_claude_code_locally(prompt, context, agent)


async def _run_claude_code_locally(prompt: str, context: Dict[str, Any], agent: str) -> str:
    """
    Execute Claude Code locally (non-Docker) for agents with requires_docker=False
    (dev_environment_setup, dev_environment_verifier only).

    Split out of run_claude_code() (#54) purely so the project_checkout lock
    decision above it can wrap this entire local execution in one
    `async with` without reindenting its whole body -- no behavior change
    versus the code that used to run inline in run_claude_code() itself.
    """
    # Re-derive these from context exactly as run_claude_code() itself does at
    # the top of that function -- this body used to run inline there and read
    # these as closure-local variables; now that it's a separate function they
    # must be recomputed here instead (pure re-reads of the same context dict,
    # so this is not a behavior change).
    obs = context.get('observability')
    task_id = context.get('task_id', 'unknown')
    project = context.get('project', 'unknown')
    mcp_servers = context.get('mcp_servers', [])

    # Only reach here if use_docker=False (dev_environment_setup and dev_environment_verifier only)
    logger.warning(f"Running agent {agent} locally (not in Docker) - this should ONLY be dev_environment_setup or dev_environment_verifier!")

    # Prepare working directory. Refuses a missing work_dir rather than running
    # the agent in the orchestrator's own cwd -- see _require_work_dir(). In
    # practice run_claude_code() has already resolved the same value for its lock
    # decision before calling here, so this is a second read of an already-
    # validated field, not a new failure point.
    work_dir = _require_work_dir(context, agent)
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
            all_assistant_turns = []  # every assistant turn's text, in order (see resolve_final_output)
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
                                all_assistant_turns.append(''.join(turn_parts))
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
                                                   model=claude_model,
                                                   pipeline_run_id=_local_pipeline_run_id or None)

                if process.returncode == 0:
                    result_text = resolve_final_output(all_assistant_turns, ''.join(result_parts))
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
    except (CancellationError, ClaudeCodeRateLimitError):
        # Never re-wrap: agent_executor.py's retry loop (and the agent-level
        # wrappers above it, e.g. base_maker_agent.py) do isinstance() checks on
        # these ("never retry cancellations", "systemic token limit, not an agent
        # failure") that only work if the original exception type survives.
        # NOTE: this only guards the non-Docker local execution path below (used
        # only by dev_environment_setup) — the Docker path returns directly above
        # and is never wrapped here in the first place. The local path also does
        # not currently run its JSON stream through ClaudeCodeBreaker.detect_from_
        # event() the way docker_runner.py's containerized path does, so this
        # exception is not actually raised from here yet; this guard is
        # forward-looking consistency, not a claim that local-path rate limits
        # are detected.
        raise
    except Exception as e:
        logger.error(f"Claude Code integration error: {str(e)}", exc_info=True)
        raise Exception(f"Claude Code integration error: {str(e)}")