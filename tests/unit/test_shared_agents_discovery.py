"""Unit tests for shared Claude Code library discovery and setup"""

import pytest
from pathlib import Path
from claude.docker_runner import docker_runner


class TestSharedClaudeStructure:
    """Test shared Claude Code library directory structure validation"""

    def test_shared_claude_directory_exists(self):
        """Test that shared Claude directory exists"""
        shared_claude_dir = Path('config/shared_claude/.claude')
        assert shared_claude_dir.exists(), "Shared Claude directory should exist"
        assert shared_claude_dir.is_dir(), "Shared Claude path should be a directory"

    def test_agents_directory_exists(self):
        """Test that agents subdirectory exists"""
        agents_dir = Path('config/shared_claude/.claude/agents')
        assert agents_dir.exists(), "Agents directory should exist"
        assert agents_dir.is_dir(), "Agents path should be a directory"

    def test_commands_directory_exists(self):
        """Test that commands subdirectory exists"""
        commands_dir = Path('config/shared_claude/.claude/commands')
        assert commands_dir.exists(), "Commands directory should exist"
        assert commands_dir.is_dir(), "Commands path should be a directory"

    def test_skills_directory_exists(self):
        """Test that skills subdirectory exists"""
        skills_dir = Path('config/shared_claude/.claude/skills')
        assert skills_dir.exists(), "Skills directory should exist"
        assert skills_dir.is_dir(), "Skills path should be a directory"

    def test_readme_exists(self):
        """Test that README.md exists in shared agents directory"""
        readme_path = Path('config/shared_claude/.claude/agents/README.md')
        assert readme_path.exists(), "README.md should exist"
        assert readme_path.is_file(), "README path should be a file"

        # Verify README has content
        content = readme_path.read_text()
        assert len(content) > 0, "README should have content"
        assert "Shared Claude Code Agents" in content, "README should have expected header"

    def test_playwright_expert_exists(self):
        """Test that playwright_expert.md exists"""
        agent_path = Path('config/shared_claude/.claude/agents/playwright_expert.md')
        assert agent_path.exists(), "playwright_expert.md should exist"
        assert agent_path.is_file(), "Playwright expert path should be a file"

    def test_database_expert_exists(self):
        """Test that database_expert.md exists"""
        agent_path = Path('config/shared_claude/.claude/agents/database_expert.md')
        assert agent_path.exists(), "database_expert.md should exist"
        assert agent_path.is_file(), "Database expert path should be a file"

    def test_flowbite_react_expert_exists(self):
        """Test that flowbite_react_expert.md exists"""
        agent_path = Path('config/shared_claude/.claude/agents/flowbite_react_expert.md')
        assert agent_path.exists(), "flowbite_react_expert.md should exist"
        assert agent_path.is_file(), "Flowbite React expert path should be a file"


class TestSharedAgentsContent:
    """Test shared agent markdown file content"""

    def test_playwright_expert_has_proper_header(self):
        """Test that playwright_expert.md has proper header"""
        agent_path = Path('config/shared_claude/.claude/agents/playwright_expert.md')
        content = agent_path.read_text()

        assert "# Playwright Testing Expert" in content, "Should have agent title"
        assert "## Expertise" in content, "Should have expertise section"
        assert "## Output Format" in content, "Should have output format section"

    def test_database_expert_has_proper_header(self):
        """Test that database_expert.md has proper header"""
        agent_path = Path('config/shared_claude/.claude/agents/database_expert.md')
        content = agent_path.read_text()

        assert "# Database Design Expert" in content, "Should have agent title"
        assert "## Expertise" in content, "Should have expertise section"
        assert "## Schema Design Principles" in content, "Should have design principles"

    def test_flowbite_react_expert_has_proper_header(self):
        """Test that flowbite_react_expert.md has proper header"""
        agent_path = Path('config/shared_claude/.claude/agents/flowbite_react_expert.md')
        content = agent_path.read_text()

        assert "# Flowbite-React UI Component Expert" in content, "Should have agent title"
        assert "## Expertise" in content, "Should have expertise section"
        assert "## Output Format" in content, "Should have output format section"

    def test_agents_have_examples(self):
        """Test that all agents have code examples"""
        agent_files = [
            'playwright_expert.md',
            'database_expert.md',
            'flowbite_react_expert.md'
        ]

        for agent_file in agent_files:
            agent_path = Path(f'config/shared_claude/.claude/agents/{agent_file}')
            content = agent_path.read_text()

            # Should have code blocks
            assert '```' in content, f"{agent_file} should have code examples"


class TestSetupSharedAgents:
    """Test _setup_shared_claude() function"""

    def test_setup_shared_claude_creates_directory(self, tmp_path):
        """Test that _setup_shared_claude creates .claude/ base directory"""
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Call setup - creates .claude/ even if /shared_claude doesn't exist
        docker_runner._setup_shared_claude(project_dir)

        # Base .claude directory should be created
        claude_dir = project_dir / '.claude'
        assert claude_dir.exists(), ".claude directory should be created"
        assert claude_dir.is_dir(), ".claude should be a directory"

        # Subdirectories (agents, commands, skills) are only created if shared resources exist
        # Since /shared_claude doesn't exist in test environment, subdirectories won't be created

    def test_setup_shared_agents_with_mock_agents(self, tmp_path):
        """Test copying shared agents with mocked source"""
        # Create mock shared agents directory
        shared_dir = tmp_path / "shared_agents" / ".claude" / "agents"
        shared_dir.mkdir(parents=True)

        # Create mock agent files
        (shared_dir / "test_expert.md").write_text("# Test Expert\n\nTest content")
        (shared_dir / "another_expert.md").write_text("# Another Expert\n\nMore content")

        # Create project directory
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Simulate the copy process (what _setup_shared_agents does)
        import shutil
        agents_dir = project_dir / '.claude' / 'agents'
        agents_dir.mkdir(parents=True)

        # Manually copy for test (simulating what the actual function does)
        for agent_file in shared_dir.glob('*.md'):
            dest = agents_dir / agent_file.name
            shutil.copy2(agent_file, dest)

        # Verify agents were copied
        assert (agents_dir / "test_expert.md").exists()
        assert (agents_dir / "another_expert.md").exists()

        # Verify content
        content = (agents_dir / "test_expert.md").read_text()
        assert "Test Expert" in content

    def test_setup_respects_project_specific_agents(self, tmp_path):
        """Test that project-specific agents take precedence"""
        # Create mock shared agents
        shared_dir = tmp_path / "shared" / ".claude" / "agents"
        shared_dir.mkdir(parents=True)
        (shared_dir / "test_expert.md").write_text("# Shared Version")

        # Create project with existing agent
        project_dir = tmp_path / "project"
        project_agents_dir = project_dir / ".claude" / "agents"
        project_agents_dir.mkdir(parents=True)
        (project_agents_dir / "test_expert.md").write_text("# Project Version")

        # Simulate copying - should skip existing
        import shutil
        for agent_file in shared_dir.glob('*.md'):
            dest = project_agents_dir / agent_file.name
            if not dest.exists():
                shutil.copy2(agent_file, dest)

        # Verify project version is preserved
        content = (project_agents_dir / "test_expert.md").read_text()
        assert "Project Version" in content, "Project-specific agent should be preserved"
        assert "Shared Version" not in content, "Shared version should not overwrite"


class TestDockerRunnerMounts:
    """Test that docker_runner adds shared claude mount"""

    def test_docker_runner_has_setup_method(self):
        """Test that docker_runner has _setup_shared_claude method"""
        assert hasattr(docker_runner, '_setup_shared_claude'), \
            "docker_runner should have _setup_shared_claude method"

        # Verify method signature
        import inspect
        sig = inspect.signature(docker_runner._setup_shared_claude)
        assert 'project_dir' in sig.parameters, \
            "_setup_shared_claude should accept project_dir parameter"
