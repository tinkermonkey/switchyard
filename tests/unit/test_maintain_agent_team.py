"""
Unit tests for Agent Team Maintainer

Tests the foundation components:
- Project discovery
- Change detection
- State tracking (per-project)
"""

import pytest
import tempfile
import yaml
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.maintain_agent_team import (
    discover_projects_for_generation,
    detect_codebase_changes,
    calculate_codebase_hash,
    load_project_state,
    save_project_state,
    ensure_directories,
    STATE_DIR
)


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace with mock projects"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create mock projects
    projects = ['context-studio', 'documentation_robotics', 'clauditoreum']
    for project in projects:
        project_dir = workspace / project
        project_dir.mkdir()

        # Add some files
        (project_dir / 'README.md').write_text(f"# {project}")
        (project_dir / 'requirements.txt').write_text("fastapi==0.109.0\npydantic==2.5.0")

    return workspace


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    """Configure temporary directories for tests"""
    state_dir = tmp_path / "state" / "projects"
    state_dir.mkdir(parents=True)

    # Monkeypatch the module-level constants
    import scripts.maintain_agent_team as module
    monkeypatch.setattr(module, 'STATE_DIR', state_dir)

    return {
        'state_dir': state_dir
    }


class TestProjectDiscovery:
    """Tests for project discovery functionality"""

    @patch('scripts.maintain_agent_team.config_manager')
    @patch('scripts.maintain_agent_team.get_workspace_root')
    def test_discover_all_projects(self, mock_workspace, mock_config, temp_workspace):
        """Test discovering all visible projects"""
        mock_workspace.return_value = temp_workspace
        mock_config.list_visible_projects.return_value = [
            'context-studio',
            'documentation_robotics',
            'clauditoreum'
        ]

        projects = discover_projects_for_generation()

        # Should exclude clauditoreum (orchestrator itself)
        assert len(projects) == 2
        assert 'context-studio' in projects
        assert 'documentation_robotics' in projects
        assert 'clauditoreum' not in projects

    @patch('scripts.maintain_agent_team.config_manager')
    @patch('scripts.maintain_agent_team.get_workspace_root')
    def test_discover_specific_project(self, mock_workspace, mock_config, temp_workspace):
        """Test discovering a specific project"""
        mock_workspace.return_value = temp_workspace

        projects = discover_projects_for_generation('context-studio')

        assert len(projects) == 1
        assert 'context-studio' in projects

    @patch('scripts.maintain_agent_team.config_manager')
    @patch('scripts.maintain_agent_team.get_workspace_root')
    def test_discover_nonexistent_project(self, mock_workspace, mock_config, temp_workspace):
        """Test discovering a project that doesn't exist"""
        mock_workspace.return_value = temp_workspace

        projects = discover_projects_for_generation('nonexistent')

        assert len(projects) == 0


class TestChangeDetection:
    """Tests for codebase change detection"""

    @patch('scripts.maintain_agent_team.get_workspace_root')
    def test_calculate_hash_basic(self, mock_workspace, temp_workspace):
        """Test basic hash calculation"""
        mock_workspace.return_value = temp_workspace
        project = 'context-studio'

        hash1 = calculate_codebase_hash(project)
        hash2 = calculate_codebase_hash(project)

        # Same hash for same content
        assert hash1 == hash2
        assert len(hash1) == 16  # First 16 chars of SHA256

    @patch('scripts.maintain_agent_team.get_workspace_root')
    def test_calculate_hash_detects_change(self, mock_workspace, temp_workspace):
        """Test that hash changes when files change"""
        mock_workspace.return_value = temp_workspace
        project = 'context-studio'
        project_dir = temp_workspace / project

        hash1 = calculate_codebase_hash(project)

        # Modify requirements.txt
        (project_dir / 'requirements.txt').write_text("fastapi==0.110.0\npydantic==2.6.0")

        hash2 = calculate_codebase_hash(project)

        assert hash1 != hash2

    @patch('scripts.maintain_agent_team.get_workspace_root')
    def test_calculate_hash_missing_project(self, mock_workspace, temp_workspace):
        """Test hash calculation for missing project"""
        mock_workspace.return_value = temp_workspace

        hash_value = calculate_codebase_hash('nonexistent')

        assert hash_value == "missing"

    @patch('scripts.maintain_agent_team.load_project_state')
    @patch('scripts.maintain_agent_team.calculate_codebase_hash')
    def test_detect_initial_generation(self, mock_hash, mock_state):
        """Test detection of initial generation (no previous state)"""
        mock_hash.return_value = "abc123"
        mock_state.return_value = {
            'codebase': {}
        }

        result = detect_codebase_changes('context-studio')

        assert result['changed'] is True
        assert result['hash'] == "abc123"
        assert result['previous_hash'] is None
        assert result['impact']['level'] == 'high'
        assert 'Initial generation' in result['impact']['reason']

    @patch('scripts.maintain_agent_team.load_project_state')
    @patch('scripts.maintain_agent_team.calculate_codebase_hash')
    def test_detect_no_changes(self, mock_hash, mock_state):
        """Test detection when no changes exist"""
        mock_hash.return_value = "abc123"
        mock_state.return_value = {
            'codebase': {
                'analysis_hash': "abc123"
            }
        }

        result = detect_codebase_changes('context-studio')

        assert result['changed'] is False
        assert result['hash'] == "abc123"
        assert result['previous_hash'] == "abc123"
        assert result['impact']['level'] == 'none'
        assert 'No changes' in result['impact']['reason']

    @patch('scripts.maintain_agent_team.load_project_state')
    @patch('scripts.maintain_agent_team.calculate_codebase_hash')
    def test_detect_codebase_modified(self, mock_hash, mock_state):
        """Test detection when codebase is modified"""
        mock_hash.return_value = "def456"
        mock_state.return_value = {
            'codebase': {
                'analysis_hash': "abc123"
            }
        }

        result = detect_codebase_changes('context-studio')

        assert result['changed'] is True
        assert result['hash'] == "def456"
        assert result['previous_hash'] == "abc123"
        assert result['impact']['level'] == 'medium'
        assert 'modified' in result['impact']['reason']


class TestStateTracking:
    """Tests for project state tracking"""

    def test_load_state_empty(self, temp_config):
        """Test loading state when it doesn't exist"""
        state = load_project_state('context-studio')

        assert state['version'] == '1.0'
        assert state['project'] == 'context-studio'
        assert state['last_updated'] is None
        assert state['codebase']['analysis_hash'] is None
        assert state['generations'] == []
        assert state['artifacts']['agents'] == []
        assert state['artifacts']['skills'] == []

    def test_save_and_load_state(self, temp_config):
        """Test saving and loading state"""
        state = {
            'version': '1.0',
            'project': 'context-studio',
            'last_updated': None,
            'codebase': {
                'analysis_hash': 'abc123',
                'analysis_timestamp': '2026-02-12T14:00:00Z',
                'tech_stack': {
                    'backend': 'python',
                    'frontend': 'react'
                }
            },
            'generations': [
                {
                    'id': 'gen-001',
                    'timestamp': '2026-02-12T14:30:00Z',
                    'success': True
                }
            ],
            'artifacts': {
                'agents': ['context-studio-tester'],
                'skills': ['context-studio-test']
            },
            'maintenance': {
                'next_analysis': None,
                'auto_regenerate': False
            }
        }

        save_project_state('context-studio', state)
        loaded = load_project_state('context-studio')

        assert loaded['project'] == 'context-studio'
        assert loaded['codebase']['analysis_hash'] == 'abc123'
        assert len(loaded['generations']) == 1
        assert loaded['generations'][0]['id'] == 'gen-001'
        assert len(loaded['artifacts']['agents']) == 1

    def test_save_state_updates_timestamp(self, temp_config):
        """Test that save_project_state updates last_updated timestamp"""
        state = {
            'version': '1.0',
            'project': 'context-studio',
            'last_updated': None,
            'codebase': {},
            'generations': [],
            'artifacts': {'agents': [], 'skills': []},
            'maintenance': {}
        }

        save_project_state('context-studio', state)
        loaded = load_project_state('context-studio')

        # Timestamp should be set
        assert loaded['last_updated'] is not None

    def test_save_state_creates_directory(self, temp_config):
        """Test that save_project_state creates project directory if needed"""
        state_dir = temp_config['state_dir']

        state = {
            'version': '1.0',
            'project': 'new-project',
            'codebase': {},
            'generations': [],
            'artifacts': {'agents': [], 'skills': []},
            'maintenance': {}
        }

        save_project_state('new-project', state)

        # Directory should exist
        assert (state_dir / 'new-project').exists()
        assert (state_dir / 'new-project' / 'agent_generation_state.yaml').exists()

    def test_state_yaml_injection_protection(self, temp_config):
        """Test that state YAML injection is prevented by safe_dump"""
        # Attempt to inject malicious YAML tag in state
        malicious_state = {
            'version': '1.0',
            'project': 'test-project',
            'codebase': {
                'analysis_hash': '!<tag:yaml.org,2002:python/object/apply:os.system> ["evil"]'
            },
            'generations': [],
            'artifacts': {'agents': [], 'skills': []},
            'maintenance': {}
        }

        save_project_state('test-project', malicious_state)

        # Load it back - should be safe (string, not code)
        loaded = load_project_state('test-project')
        assert loaded['project'] == 'test-project'

        # The malicious string should be loaded as a plain string, not executed
        hash_value = loaded['codebase']['analysis_hash']
        assert isinstance(hash_value, str)
        # The value is preserved as a string (not executed)
        assert 'tag:yaml.org' in hash_value

        # Verify it's a string, not an executed command
        assert type(hash_value) == str


class TestDirectorySetup:
    """Tests for directory initialization"""

    def test_ensure_directories(self, temp_config):
        """Test that ensure_directories creates required directories"""
        # Remove directories to test creation
        import shutil
        state_dir = temp_config['state_dir']
        if state_dir.exists():
            shutil.rmtree(state_dir)

        ensure_directories()

        assert temp_config['state_dir'].exists()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
