import subprocess
import json
import os
import logging
from typing import Dict, Any
from pathlib import Path
from claude.docker_runner import docker_runner
from services.project_workspace import workspace_manager

logger = logging.getLogger(__name__)

async def run_claude_code(prompt: str, context: Dict[str, Any]) -> str:
    """Execute Claude Code with given prompt and context"""
    import time

    logger.info("run_claude_code called")
    logger.info(f"Context project: {context.get('project', 'unknown')}")
    logger.info(f"Prompt length: {len(prompt)}")

    # Get observability manager from context
    obs = context.get('observability')
    task_id = context.get('task_id', 'unknown')
    agent = context.get('agent', 'unknown')
    project = context.get('project', 'unknown')

    # Emit prompt constructed event
    if obs:
        obs.emit_prompt_constructed(agent, task_id, project, prompt)

    # Get MCP server configuration from context
    mcp_servers = context.get('mcp_servers', [])

    # Determine if this is a project-specific task that should run in Docker
    use_docker = context.get('use_docker', False)
    if use_docker and project != 'unknown':
        logger.info(f"Running agent in Docker container for project {project}")

        # Get project directory from workspace manager
        project_dir = workspace_manager.get_project_dir(project)

        if not project_dir.exists():
            raise Exception(f"Project directory does not exist: {project_dir}")

        # Run in Docker container
        return await docker_runner.run_agent_in_container(
            prompt=prompt,
            context=context,
            project_dir=project_dir,
            mcp_servers=mcp_servers,
            stream_callback=context.get('stream_callback')
        )

    # Fall back to local execution (for orchestrator-internal tasks)
    logger.info("Running agent locally (not in Docker)")

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

            cmd = [
                'claude',
                '--print',
                '--verbose',
                '--output-format', 'stream-json',
                '--model', claude_model,
                '--dangerously-skip-permissions',
                prompt
            ]

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

            # Set up environment with API keys
            env = os.environ.copy()
            if 'CONTEXT7_API_KEY' in os.environ:
                env['CONTEXT7_API_KEY'] = os.environ['CONTEXT7_API_KEY']

            if 'ANTHROPIC_API_KEY' not in env:
                logger.error("ANTHROPIC_API_KEY not found in environment")
                raise Exception("ANTHROPIC_API_KEY is required to run Claude Code")

            logger.info(f"Executing Claude CLI in {work_dir}")
            logger.info(f"Command: {' '.join(cmd[:3])}...")  # Don't log full prompt

            # Emit Claude API call started event
            api_start_time = time.time()
            if obs:
                obs.emit_claude_call_started(agent, task_id, project, claude_model)

            # Get stream callback if websocket is connected
            stream_callback = context.get('stream_callback')

            # Use Popen to stream output in real-time
            process = subprocess.Popen(
                cmd,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                stdin=subprocess.DEVNULL
            )

            # Collect all output for final result
            result_parts = []
            input_tokens = 0
            output_tokens = 0

            # Stream output line by line
            try:
                logger.info(f"Starting to stream Claude output, callback present: {stream_callback is not None}")
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break

                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                        event_type = event.get('type', 'unknown')

                        # Log event structure for debugging
                        if event_type not in ['progress', 'status']:
                            logger.debug(f"Stream event type: {event_type}, keys: {list(event.keys())}")

                        # Stream event to websocket if callback provided
                        if stream_callback:
                            stream_callback(event)

                        # Track token usage from stream events
                        if 'usage' in event:
                            input_tokens = event['usage'].get('input_tokens', input_tokens)
                            output_tokens = event['usage'].get('output_tokens', output_tokens)

                        # Collect result text from various event types
                        # Claude Code stream-json format has several event types with text:
                        # - "assistant" events have message.content[].text
                        # - "result" events have a "result" string field
                        text_collected = False

                        if event_type == 'assistant':
                            # Extract text from assistant message content
                            message = event.get('message', {})
                            content = message.get('content', [])
                            for item in content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
                                    if text:
                                        result_parts.append(text)
                                        text_collected = True
                        elif event_type == 'result':
                            # Extract from result field (final summary)
                            if isinstance(event.get('result'), str):
                                result_parts.append(event['result'])
                                text_collected = True

                        if text_collected:
                            logger.debug(f"Collected text from {event_type} event, total length: {sum(len(p) for p in result_parts)}")

                    except json.JSONDecodeError:
                        # Non-JSON output, just log it
                        logger.debug(f"Non-JSON output: {line[:100]}")

                # Wait for process to complete
                process.wait(timeout=600)
                api_duration_ms = (time.time() - api_start_time) * 1000

                # Emit completion event
                if obs:
                    obs.emit_claude_call_completed(agent, task_id, project, api_duration_ms,
                                                   input_tokens, output_tokens)

                if process.returncode == 0:
                    result_text = ''.join(result_parts)
                    logger.info(f"Claude CLI completed successfully, result length: {len(result_text)}")
                    return result_text
                else:
                    stderr = process.stderr.read() if process.stderr else "Unknown error"
                    logger.error(f"Claude CLI failed: {stderr}")
                    raise Exception(f"Claude Code failed: {stderr}")

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