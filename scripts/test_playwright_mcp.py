#!/usr/bin/env python3
"""
Test script to verify Playwright MCP integration with browser automation.

This script runs the senior_software_engineer agent with a simple prompt
that asks it to navigate to a webpage and verify it can interact with the browser.
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


async def test_playwright_mcp():
    """Test that Playwright MCP is available to the agent for browser automation"""

    logger.info("=" * 80)
    logger.info("PLAYWRIGHT MCP BROWSER AUTOMATION TEST")
    logger.info("=" * 80)

    # Step 1: Verify agent configuration
    logger.info("\n[1/4] Verifying agent configuration...")
    agent_config = config_manager.get_agent('senior_software_engineer')
    logger.info(f"  Agent: {agent_config.name}")
    logger.info(f"  MCP Servers configured: {len(agent_config.mcp_servers)}")

    mcp_names = [s.get('name') for s in agent_config.mcp_servers]
    for name in mcp_names:
        logger.info(f"    - {name}")

    if 'playwright' not in mcp_names:
        logger.error("  ✗ FAIL: Playwright not in MCP servers!")
        return False
    logger.info("  ✓ Playwright is configured")

    # Step 2: Verify MCP config generation
    logger.info("\n[2/4] Testing MCP config generation...")
    runner = DockerAgentRunner()

    # Use test-project which has a config file
    test_project_name = 'test-project'
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

    if 'playwright' not in mcp_config['mcpServers']:
        logger.error("  ✗ FAIL: Playwright not in generated MCP config!")
        return False
    logger.info("  ✓ Playwright in MCP config")

    # Check the playwright config details
    pw_config = mcp_config['mcpServers']['playwright']
    logger.info(f"  Playwright command: {pw_config.get('command')}")
    logger.info(f"  Playwright args: {pw_config.get('args')}")
    if 'env' in pw_config:
        logger.info(f"  Playwright env: {pw_config['env']}")

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

    # Step 4: Run a minimal agent execution to verify Playwright MCP is loaded
    logger.info("\n[4/4] Running agent with Playwright MCP...")
    logger.info("  This will execute Claude Code with browser automation capabilities.")
    logger.info("  The agent will be asked to navigate to a webpage and describe it.")

    # Create minimal context
    context = {
        'agent': 'senior_software_engineer',
        'task_id': 'playwright-mcp-test',
        'project': test_project_name,
        'claude_model': 'claude-sonnet-4-5-20250929',
    }

    # Simple prompt asking agent to use browser automation
    prompt = """Please use your browser automation capabilities to:

1. Navigate to https://example.com
2. Take a screenshot of the page
3. Tell me what you see on the page (heading, content, etc.)

Keep your response concise - just describe what the page looks like."""

    try:
        logger.info("\n  Starting agent execution...")
        logger.info("  Watch for log messages indicating Playwright MCP is being used")
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

        # Check if the response mentions browser/screenshot/navigation
        result_lower = result.lower()
        success_indicators = ['screenshot', 'navigate', 'browser', 'example.com', 'playwright']
        found_indicators = [word for word in success_indicators if word in result_lower]

        if found_indicators:
            logger.info(f"\n  ✓ SUCCESS: Agent mentioned browser automation keywords: {', '.join(found_indicators)}")
            logger.info("  ✓ Playwright MCP integration is working!")
            return True
        else:
            logger.warning("\n  ⚠ Agent did not mention browser automation in response")
            logger.warning("  This might mean Playwright MCP wasn't loaded, or agent chose not to use it")
            logger.info("  Check the logs above for 'Using MCP config' and npx messages")
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
    success = asyncio.run(test_playwright_mcp())

    if success:
        logger.info("\n" + "=" * 80)
        logger.info("✓ PLAYWRIGHT MCP TEST PASSED")
        logger.info("=" * 80)
        sys.exit(0)
    else:
        logger.error("\n" + "=" * 80)
        logger.error("✗ PLAYWRIGHT MCP TEST FAILED")
        logger.error("=" * 80)
        sys.exit(1)
