import subprocess
import json
import os
from typing import Dict, Any
from pathlib import Path

async def run_claude_code(prompt: str, context: Dict[str, Any]) -> str:
    """Execute Claude Code with given prompt and context"""

    # Prepare working directory
    work_dir = Path(context.get('work_dir', '.'))

    # Get MCP server configuration from context
    mcp_servers = context.get('mcp_servers', [])

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
        result = subprocess.run(['which', 'claude'], capture_output=True, text=True)

        if result.returncode == 0:
            # Claude CLI is available - use it
            cmd = [
                'claude',
                '-p', prompt
            ]

            # Add MCP server configurations
            for server in mcp_servers:
                if server['name'] == 'context7':
                    cmd.extend(['--mcp-server', f"context7:http:{server['url']}"])
                elif server['name'] == 'serena':
                    cmd.extend(['--mcp-server', f"serena:http:{server['url']}"])
                elif server['name'] == 'puppeteer':
                    cmd.extend(['--mcp-server', f"puppeteer:http:{server['url']}"])

            # Ensure working directory exists or use current directory
            if not work_dir.exists():
                work_dir.mkdir(parents=True, exist_ok=True)

            # Set up environment with API keys
            env = os.environ.copy()
            if 'CONTEXT7_API_KEY' in os.environ:
                env['CONTEXT7_API_KEY'] = os.environ['CONTEXT7_API_KEY']

            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
                env=env
            )

            if result.returncode == 0:
                return result.stdout.strip()
            else:
                raise Exception(f"Claude Code failed: {result.stderr}")
        else:
            # Claude CLI not available - simulate response for development
            print("⚠️ Claude CLI not found, simulating response for development")

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
        raise Exception("Claude Code execution timed out")
    except Exception as e:
        raise Exception(f"Claude Code integration error: {str(e)}")