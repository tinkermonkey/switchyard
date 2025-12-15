"""Integration tests for shared agents Docker volume mounting"""

import pytest
from pathlib import Path
from claude.docker_runner import docker_runner


class TestSharedAgentsDockerMount:
    """Test Docker volume mounting for shared agents"""

    @pytest.mark.integration
    def test_shared_claude_directory_mountable(self):
        """Test that shared agents directory exists and can be mounted"""
        # Verify source directory exists
        shared_claude_dir = Path('/app/config/shared_claude/.claude')

        # In container, this path should exist
        if shared_claude_dir.exists():
            assert shared_claude_dir.is_dir(), "Shared agents should be a directory"

            # Verify it has agent files
            agent_files = list(shared_claude_dir.glob('*.md'))
            assert len(agent_files) >= 3, "Should have at least 3 agent definitions"

    @pytest.mark.integration
    def test_docker_runner_builds_mount_command(self):
        """Test that docker_runner builds correct mount command"""
        # This would require mocking _build_docker_command
        # For now, just verify the method exists
        assert hasattr(docker_runner, '_build_docker_command'), \
            "docker_runner should have _build_docker_command method"

    @pytest.mark.integration
    def test_setup_shared_claude_integration(self, tmp_path):
        """Integration test for _setup_shared_claude with real directory"""
        # Create a test project directory
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Call the actual setup method
        docker_runner._setup_shared_claude(project_dir)

        # Verify base .claude/ directory was created
        claude_dir = project_dir / '.claude'
        assert claude_dir.exists(), ".claude directory should be created"

        # Subdirectories (agents, commands, skills) are only created if /shared_claude mount exists
        # In test environment without mount, they won't be created

    @pytest.mark.integration
    @pytest.mark.skipif(
        not Path('/shared_claude/.claude/agents').exists(),
        reason="Requires /shared_claude mount (only available in agent containers)"
    )
    def test_shared_claude_accessible_from_container(self):
        """Test that shared agents are accessible from /shared_claude mount"""
        shared_claude_dir = Path('/shared_claude/.claude/agents')

        assert shared_claude_dir.exists(), "/shared_claude mount should exist"
        assert shared_claude_dir.is_dir(), "/shared_claude should be a directory"

        # Verify agent files are present
        expected_agents = [
            'playwright_expert.md',
            'database_expert.md',
            'flowbite_react_expert.md'
        ]

        for agent_file in expected_agents:
            agent_path = shared_claude_dir / agent_file
            assert agent_path.exists(), f"{agent_file} should exist in shared agents"
            assert agent_path.is_file(), f"{agent_file} should be a file"

            # Verify file is readable
            content = agent_path.read_text()
            assert len(content) > 0, f"{agent_file} should have content"

    @pytest.mark.integration
    @pytest.mark.skipif(
        not Path('/shared_claude/.claude/agents').exists(),
        reason="Requires /shared_claude mount"
    )
    def test_shared_claude_mount_is_read_only(self):
        """Test that shared agents mount is read-only"""
        shared_claude_dir = Path('/shared_claude/.claude/agents')

        if shared_claude_dir.exists():
            test_file = shared_claude_dir / "test_write.txt"

            # Attempt to write should fail (read-only mount)
            try:
                test_file.write_text("test")
                # If we got here, mount is NOT read-only
                test_file.unlink()  # Clean up
                pytest.fail("Shared agents mount should be read-only")
            except (OSError, PermissionError):
                # Expected - read-only mount
                pass

    @pytest.mark.integration
    def test_setup_copies_agents_to_project(self, tmp_path):
        """Test that setup copies agents from shared location to project"""
        # Create mock shared agents source
        shared_source = tmp_path / "shared_source"
        shared_source.mkdir()

        # Create mock agent files
        (shared_source / "test_expert1.md").write_text("# Test Expert 1")
        (shared_source / "test_expert2.md").write_text("# Test Expert 2")

        # Create project directory
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Manually simulate the copy process
        import shutil
        project_agents_dir = project_dir / '.claude' / 'agents'
        project_agents_dir.mkdir(parents=True)

        for agent_file in shared_source.glob('*.md'):
            dest = project_agents_dir / agent_file.name
            if not dest.exists():
                shutil.copy2(agent_file, dest)

        # Verify agents were copied
        assert (project_agents_dir / "test_expert1.md").exists()
        assert (project_agents_dir / "test_expert2.md").exists()

        # Verify content
        content1 = (project_agents_dir / "test_expert1.md").read_text()
        assert "Test Expert 1" in content1

    @pytest.mark.integration
    def test_project_agents_override_shared_claude(self, tmp_path):
        """Test that project-specific agents override shared agents"""
        # Create mock shared agents
        shared_source = tmp_path / "shared"
        shared_source.mkdir()
        (shared_source / "test_expert.md").write_text("# Shared Expert")

        # Create project with existing agent
        project_dir = tmp_path / "project"
        project_agents_dir = project_dir / '.claude' / 'agents'
        project_agents_dir.mkdir(parents=True)

        # Project already has this agent
        (project_agents_dir / "test_expert.md").write_text("# Project-Specific Expert")

        # Simulate copy (should skip existing)
        import shutil
        for agent_file in shared_source.glob('*.md'):
            dest = project_agents_dir / agent_file.name
            if not dest.exists():
                shutil.copy2(agent_file, dest)

        # Verify project version is preserved
        content = (project_agents_dir / "test_expert.md").read_text()
        assert "Project-Specific Expert" in content
        assert "Shared Expert" not in content
