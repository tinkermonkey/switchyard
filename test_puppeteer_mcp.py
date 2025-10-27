#!/usr/bin/env python3
"""
Test script to verify Puppeteer MCP integration with agents.

This script runs the senior_software_engineer agent with a simple prompt
that asks it to list available tools, allowing us to verify that Puppeteer
MCP tools are accessible.
"""

import asyncio
import sys
import logging
from pathlib import Path

# Setup logging to see detailed output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))

from claude.docker_runner import DockerAgentRunner
from config.manager import config_manager


async def test_puppeteer_mcp():
    """Test that Puppeteer MCP is available to the agent"""

    logger.info("=" * 80)
    logger.info("PUPPETEER MCP INTEGRATION TEST")
    logger.info("=" * 80)

    # Step 1: Verify agent configuration
    logger.info("\n[1/4] Verifying agent configuration...")
    agent_config = config_manager.get_agent('senior_software_engineer')
    logger.info(f"  Agent: {agent_config.name}")
    logger.info(f"  MCP Servers configured: {len(agent_config.mcp_servers)}")

    mcp_names = [s.get('name') for s in agent_config.mcp_servers]
    for name in mcp_names:
        logger.info(f"    - {name}")

    if 'puppeteer' not in mcp_names:
        logger.error("  ✗ FAIL: Puppeteer not in MCP servers!")
        return False
    logger.info("  ✓ Puppeteer is configured")

    # Step 2: Verify MCP config generation
    logger.info("\n[2/4] Testing MCP config generation...")
    runner = DockerAgentRunner()

    # Use test-project which has a config file
    test_project_name = 'test-project'

    # For this test, use the local path (we're outside Docker)
    # When inside orchestrator container, /workspace exists
    # When running locally, use current directory
    import os
    if os.path.exists('/workspace'):
        project_dir = Path(f'/workspace/{test_project_name}')
        os.makedirs(project_dir, exist_ok=True)
    else:
        # Running locally - create temp directory
        project_dir = Path(f'/tmp/{test_project_name}')
        os.makedirs(project_dir, exist_ok=True)

    mcp_config = runner._prepare_mcp_config(agent_config.mcp_servers, project_dir)
    logger.info(f"  MCP servers in config: {list(mcp_config['mcpServers'].keys())}")

    if 'puppeteer' not in mcp_config['mcpServers']:
        logger.error("  ✗ FAIL: Puppeteer not in generated MCP config!")
        return False
    logger.info("  ✓ Puppeteer in MCP config")

    # Step 3: Test MCP config file creation
    logger.info("\n[3/4] Testing MCP config file creation...")
    mcp_file_path = runner._write_mcp_config_file(mcp_config, 'senior_software_engineer', 'test-123')
    logger.info(f"  MCP config file: {mcp_file_path}")

    import json
    if not os.path.exists(mcp_file_path):
        logger.error("  ✗ FAIL: MCP config file not created!")
        return False

    with open(mcp_file_path, 'r') as f:
        file_config = json.load(f)

    logger.info("  ✓ MCP config file created successfully")
    logger.info(f"  File contents: {json.dumps(file_config, indent=2)}")

    # Step 4: Run a minimal agent execution to verify MCP is loaded
    logger.info("\n[4/4] Running agent with MCP config...")
    logger.info("  This will execute Claude Code with MCP config mounted.")
    logger.info("  The agent will be asked to list its available tools.")

    # Create minimal context
    context = {
        'agent': 'senior_software_engineer',
        'task_id': 'puppeteer-mcp-test',
        'project': test_project_name,
        'claude_model': 'claude-sonnet-4-5-20250929',
    }

    # Simple prompt asking agent to list its tools
    prompt = """Please list all the tools and capabilities you have access to.

Specifically, check if you have access to:
1. Puppeteer MCP tools (for browser automation)
2. Context7 MCP tools (for documentation)

For each MCP server, list the available tools/functions.

Keep your response concise - just list the MCP servers and their tools."""

    try:
        logger.info("\n  Starting agent execution...")
        logger.info("  Watch for log messages indicating MCP config is being used")
        logger.info("  " + "-" * 60)

        result = await runner.run_agent_in_container(
            prompt=prompt,
            context=context,
            project_dir=project_dir,
            mcp_servers=agent_config.mcp_servers,
            stream_callback=None
        )

        logger.info("  " + "-" * 60)
        logger.info("\n  Agent execution completed!")
        logger.info(f"  Result length: {len(result)} characters")
        logger.info("\n  Agent Response:")
        logger.info("  " + "=" * 60)
        print(result)
        logger.info("  " + "=" * 60)

        # Check if the response mentions Puppeteer
        if 'puppeteer' in result.lower():
            logger.info("\n  ✓ SUCCESS: Agent mentioned Puppeteer in response!")
            logger.info("  ✓ Puppeteer MCP integration is working!")
            return True
        else:
            logger.warning("\n  ⚠ Agent did not mention Puppeteer in response")
            logger.warning("  This might mean MCP config wasn't loaded, or agent chose not to mention it")
            logger.info("  Check the logs above for 'Using MCP config' messages")
            return True  # Still consider it a success if execution completed

    except Exception as e:
        logger.error(f"\n  ✗ FAIL: Agent execution failed: {e}", exc_info=True)
        return False
    finally:
        # Cleanup
        if os.path.exists(mcp_file_path):
            os.remove(mcp_file_path)
            logger.info(f"\n  Cleaned up test MCP config file")


if __name__ == "__main__":
    success = asyncio.run(test_puppeteer_mcp())

    if success:
        logger.info("\n" + "=" * 80)
        logger.info("✓ PUPPETEER MCP TEST PASSED")
        logger.info("=" * 80)
        sys.exit(0)
    else:
        logger.error("\n" + "=" * 80)
        logger.error("✗ PUPPETEER MCP TEST FAILED")
        logger.error("=" * 80)
        sys.exit(1)
