#!/usr/bin/env python3
"""
Test Agent Filesystem Access

Validates that agents can write to the workspace without actually launching an agent.
This helps diagnose permission issues before running expensive agent operations.
"""

import sys
import os
import logging
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.manager import config_manager
from services.project_workspace import workspace_manager
from claude.docker_runner import DockerAgentRunner

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_filesystem_access(project_name: str, agent_name: str = "senior_software_engineer"):
    """
    Test filesystem access for a specific agent and project.

    Args:
        project_name: Name of the project to test
        agent_name: Name of the agent to test (default: senior_software_engineer)
    """
    logger.info("=" * 80)
    logger.info(f"TESTING FILESYSTEM ACCESS FOR: {agent_name} on {project_name}")
    logger.info("=" * 80)

    # Get project directory
    project_dir = workspace_manager.get_project_dir(project_name)
    logger.info(f"\n1. Project directory: {project_dir}")

    if not project_dir.exists():
        logger.error(f"   ❌ Project directory does not exist!")
        return False
    else:
        logger.info(f"   ✓ Project directory exists")

    # Get agent config
    agent_config = config_manager.get_project_agent_config(project_name, agent_name)
    filesystem_write_allowed = getattr(agent_config, 'filesystem_write_allowed', True)

    logger.info(f"\n2. Agent configuration:")
    logger.info(f"   Agent: {agent_name}")
    logger.info(f"   filesystem_write_allowed: {filesystem_write_allowed}")
    logger.info(f"   makes_code_changes: {getattr(agent_config, 'makes_code_changes', 'unknown')}")
    logger.info(f"   requires_dev_container: {getattr(agent_config, 'requires_dev_container', 'unknown')}")

    if not filesystem_write_allowed:
        logger.warning(f"   ⚠️  Agent is configured for READ-ONLY access")
        logger.info(f"   This is expected for reviewer agents, but NOT for dev agents")
        return True  # Not an error, just informational

    # Test write access from orchestrator container
    logger.info(f"\n3. Testing write access from orchestrator container...")

    runner = DockerAgentRunner()
    test_result = runner._verify_write_access(str(project_dir))

    if test_result['success']:
        logger.info(f"   ✓ {test_result['message']}")
    else:
        logger.error(f"   ❌ WRITE TEST FAILED: {test_result['error']}")
        return False

    # Check Docker socket access
    logger.info(f"\n4. Testing Docker socket access...")
    import subprocess
    try:
        result = subprocess.run(
            ['docker', 'ps', '--format', '{{.Names}}'],
            capture_output=True,
            text=True,
            check=True,
            timeout=5
        )
        container_count = len(result.stdout.strip().split('\n'))
        logger.info(f"   ✓ Can access Docker socket ({container_count} containers running)")
    except subprocess.CalledProcessError as e:
        logger.error(f"   ❌ Cannot access Docker socket: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"   ❌ Docker test failed: {e}")
        return False

    # Check git access
    logger.info(f"\n5. Testing git access...")
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info(f"   ✓ Can read git status")

            # Test git write access
            test_branch = f"test-access-{int(os.times().elapsed * 1000)}"
            result = subprocess.run(
                ['git', 'checkout', '-b', test_branch],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info(f"   ✓ Can create git branches")
                # Clean up
                subprocess.run(['git', 'checkout', 'main'], cwd=str(project_dir), capture_output=True)
                subprocess.run(['git', 'branch', '-D', test_branch], cwd=str(project_dir), capture_output=True)
            else:
                logger.error(f"   ❌ Cannot create git branches: {result.stderr}")
                return False
        else:
            logger.error(f"   ❌ Cannot read git status: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"   ❌ Git test failed: {e}")
        return False

    # Check file ownership
    logger.info(f"\n6. Checking file ownership...")
    try:
        import pwd
        stat_info = project_dir.stat()
        current_uid = os.getuid()

        logger.info(f"   Current UID: {current_uid}")
        logger.info(f"   Project dir owner UID: {stat_info.st_uid}")

        if current_uid == 0:
            logger.info(f"   ✓ Running as root (should map to host user in rootless Docker)")
        elif current_uid == stat_info.st_uid:
            logger.info(f"   ✓ Running as same UID as project directory owner")
        else:
            logger.warning(f"   ⚠️  UID mismatch - may cause permission issues")
    except Exception as e:
        logger.warning(f"   Could not check ownership: {e}")

    # Test actual agent container write access
    if filesystem_write_allowed:
        logger.info(f"\n7. Testing write access from INSIDE agent container...")
        logger.info(f"   (This simulates what the actual agent will experience)")

        container_test_result = _test_agent_container_write(project_name, agent_name, project_dir)

        if not container_test_result:
            logger.error(f"   ❌ Agent container CANNOT write to workspace!")
            logger.error(f"   This is the actual problem - agents will fail when they run!")
            return False
        else:
            logger.info(f"   ✓ Agent container CAN write to workspace")

    logger.info("\n" + "=" * 80)
    logger.info("✓ ALL TESTS PASSED - Agent should be able to write to workspace")
    logger.info("=" * 80)
    return True


def _test_agent_container_write(project_name: str, agent_name: str, project_dir: Path) -> bool:
    """
    Actually spawn an agent container via docker_runner to test write access.

    This uses the REAL code path that agents take - goes through docker_runner's
    Docker-in-Docker spawning from inside the orchestrator container.
    """
    import asyncio

    logger.info(f"   Using actual docker_runner to spawn test container...")

    try:
        # Import docker_runner
        from claude.docker_runner import DockerAgentRunner

        # Create test context
        test_context = {
            'agent': agent_name,
            'task_id': 'filesystem-test',
            'project': project_name,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }

        # Create simple test prompt
        test_prompt = """
You are testing filesystem write access.

CRITICAL: You MUST write a test file to verify write access.

Execute these commands:
1. `ls -la /workspace` to see permissions
2. `touch /workspace/.write-test-agent` to create a test file
3. `ls -la /workspace/.write-test-agent` to verify it was created
4. `rm /workspace/.write-test-agent` to clean up
5. Echo "FILESYSTEM_TEST_PASSED" when done

If you cannot write files, explain the exact error you get.
"""

        # Run via docker_runner (this is the real code path!)
        runner = DockerAgentRunner()

        logger.info(f"   Spawning agent container via docker_runner (real code path)...")

        # Run the agent
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                runner.run_agent_in_container(
                    prompt=test_prompt,
                    context=test_context,
                    project_dir=project_dir,
                    mcp_servers=None
                )
            )
        finally:
            loop.close()

        # Check if test passed
        logger.info(f"   Agent output length: {len(result)} chars")

        if "FILESYSTEM_TEST_PASSED" in result:
            logger.info(f"   ✓ Agent reported: FILESYSTEM_TEST_PASSED")
            return True
        elif "Permission denied" in result or "cannot touch" in result or "Read-only file system" in result:
            logger.error(f"   ❌ Agent reported permission errors")
            logger.error(f"   Agent output: {result[:500]}")
            return False
        else:
            logger.warning(f"   ⚠️  Could not determine test result from agent output")
            logger.warning(f"   Agent output preview: {result[:500]}")
            # Look for the test file on disk as fallback
            test_file = project_dir / '.write-test-agent'
            if test_file.exists():
                logger.info(f"   ✓ Found test file on disk (agent did write it)")
                test_file.unlink()  # Clean up
                return True
            else:
                logger.error(f"   ❌ Test file not found on disk (agent couldn't write)")
                return False

    except Exception as e:
        logger.error(f"   ❌ docker_runner test failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Test agent filesystem access without launching an agent"
    )
    parser.add_argument(
        "project",
        help="Project name (e.g., context-studio)"
    )
    parser.add_argument(
        "--agent",
        default="senior_software_engineer",
        help="Agent name to test (default: senior_software_engineer)"
    )

    args = parser.parse_args()

    success = test_filesystem_access(args.project, args.agent)
    sys.exit(0 if success else 1)
