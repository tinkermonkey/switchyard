#!/usr/bin/env python3
"""
Test script to verify read-only filesystem enforcement for agents

This script tests that agents with filesystem_write_allowed=false
cannot create files in their workspace.
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config.manager import config_manager
from claude.docker_runner import docker_runner
from pathlib import Path
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_readonly_enforcement():
    """Test that business_analyst agent cannot write files"""

    logger.info("=" * 80)
    logger.info("Testing Read-Only Filesystem Enforcement")
    logger.info("=" * 80)

    # Get business analyst config
    project = "context-studio"
    agent = "business_analyst"

    agent_config = config_manager.get_project_agent_config(project, agent)
    filesystem_write_allowed = getattr(agent_config, 'filesystem_write_allowed', True)

    logger.info(f"\nAgent: {agent}")
    logger.info(f"Project: {project}")
    logger.info(f"filesystem_write_allowed: {filesystem_write_allowed}")

    if filesystem_write_allowed:
        logger.error("FAIL: Agent should have filesystem_write_allowed=false")
        return False
    else:
        logger.info("Agent correctly configured with filesystem_write_allowed=false")

    # Create a test prompt that tries to create a file
    test_prompt = """
Please create a file called test_file.md with the following content:

# Test File

This is a test to see if file creation works.

Save this file in the workspace directory.
"""

    # Set up context
    context = {
        'agent': agent,
        'project': project,
        'task_id': 'test_readonly',
        'claude_model': 'claude-sonnet-4-5-20250929'
    }

    # Project directory - check if we're in container
    workspace_root = Path("/workspace")
    if not workspace_root.exists():
        # Not in container - skip this test
        logger.warning("SKIP: Not running in orchestrator container (no /workspace)")
        return True  # Skip instead of fail

    project_dir = workspace_root / project
    if not project_dir.exists():
        logger.warning(f"Project directory {project_dir} doesn't exist, creating it")
        project_dir.mkdir(parents=True, exist_ok=True)

    # Try to run agent with file creation prompt
    logger.info("\n" + "=" * 80)
    logger.info("Running agent with file creation prompt...")
    logger.info("=" * 80)

    try:
        result = await docker_runner.run_agent_in_container(
            prompt=test_prompt,
            context=context,
            project_dir=project_dir,
            mcp_servers=None
        )

        logger.info(f"\nAgent output: {result[:500]}..." if len(result) > 500 else f"\nAgent output: {result}")

        # Check if file was created
        test_file = project_dir / "test_file.md"
        if test_file.exists():
            logger.error("FAIL: File was created despite read-only mount!")
            logger.error(f"File exists at: {test_file}")
            return False
        else:
            logger.info("PASS: File was NOT created (expected behavior)")

        # Check if agent reported an error about read-only filesystem
        if "read-only" in result.lower() or "permission denied" in result.lower():
            logger.info("PASS: Agent received filesystem permission error")
        else:
            logger.warning("WARNING: Agent didn't report permission error, but file wasn't created")

        return True

    except Exception as e:
        logger.error(f"Test failed with exception: {e}")
        return False

async def test_readwrite_enforcement():
    """Test that senior_software_engineer agent CAN write files"""

    logger.info("\n" + "=" * 80)
    logger.info("Testing Read-Write Filesystem for Code Agents")
    logger.info("=" * 80)

    project = "context-studio"
    agent = "senior_software_engineer"

    agent_config = config_manager.get_project_agent_config(project, agent)
    filesystem_write_allowed = getattr(agent_config, 'filesystem_write_allowed', True)

    logger.info(f"\nAgent: {agent}")
    logger.info(f"Project: {project}")
    logger.info(f"filesystem_write_allowed: {filesystem_write_allowed}")

    if not filesystem_write_allowed:
        logger.error("FAIL: Code agent should have filesystem_write_allowed=true")
        return False
    else:
        logger.info("Agent correctly configured with filesystem_write_allowed=true")

    # Create a test prompt that creates a file
    test_prompt = """
Please create a simple test file called test_code_file.py with a hello world function:

def hello():
    print("Hello from code agent!")

Save this file in the workspace directory.
"""

    context = {
        'agent': agent,
        'project': project,
        'task_id': 'test_readwrite',
        'claude_model': 'claude-sonnet-4-5-20250929'
    }

    # Check if we're in container
    workspace_root = Path("/workspace")
    if not workspace_root.exists():
        # Not in container - skip this test
        logger.warning("SKIP: Not running in orchestrator container (no /workspace)")
        return True  # Skip instead of fail

    project_dir = workspace_root / project
    if not project_dir.exists():
        logger.warning(f"Project directory {project_dir} doesn't exist, creating it")
        project_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = await docker_runner.run_agent_in_container(
            prompt=test_prompt,
            context=context,
            project_dir=project_dir,
            mcp_servers=None
        )

        logger.info(f"\nAgent output: {result[:500]}..." if len(result) > 500 else f"\nAgent output: {result}")

        # Check if file was created
        test_file = project_dir / "test_code_file.py"
        if test_file.exists():
            logger.info("PASS: Code agent successfully created file")
            # Clean up
            test_file.unlink()
            logger.info("Cleaned up test file")
            return True
        else:
            logger.error("FAIL: Code agent couldn't create file (should be able to)")
            return False

    except Exception as e:
        logger.error(f"Test failed with exception: {e}")
        return False

if __name__ == "__main__":
    logger.info("\n" + "=" * 80)
    logger.info("Read-Only Filesystem Enforcement Test Suite")
    logger.info("=" * 80)

    # Run tests
    loop = asyncio.get_event_loop()

    # Test 1: Read-only enforcement for analysis agents
    test1_result = loop.run_until_complete(test_readonly_enforcement())

    # Test 2: Read-write for code agents
    test2_result = loop.run_until_complete(test_readwrite_enforcement())

    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("Test Summary")
    logger.info("=" * 80)
    logger.info(f"Test 1 (Read-Only Enforcement): {'PASS' if test1_result else 'FAIL'}")
    logger.info(f"Test 2 (Read-Write for Code Agents): {'PASS' if test2_result else 'FAIL'}")

    if test1_result and test2_result:
        logger.info("\nAll tests passed!")
        sys.exit(0)
    else:
        logger.error("\nSome tests failed")
        sys.exit(1)
