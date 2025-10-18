"""
Unit tests for Agent Container Recovery Service
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from services.agent_container_recovery import AgentContainerRecovery


class TestAgentContainerRecovery:
    
    def test_parse_container_name_valid(self):
        """Test parsing a valid container name"""
        recovery = AgentContainerRecovery()
        
        # Real format from docker_runner.py:
        # raw_container_name = f"claude-agent-{project}-{task_id}"
        # where task_id = f"{agent}_{project}_{board}_{issue_number}_{timestamp}"
        # Example: claude-agent-what_am_i_watching-senior_software_engineer_what_am_i_watching_SDLC-Execution_8_1760714730
        container_name = "claude-agent-what_am_i_watching-senior_software_engineer_what_am_i_watching_SDLC-Execution_8_1760714730"
        
        result = recovery.parse_container_name(container_name)
        
        assert result is not None, f"Failed to parse container name, got: {result}"
        assert result['agent'] == 'senior_software_engineer'
        assert result['project'] == 'what_am_i_watching'
        assert result['board'] == 'SDLC-Execution'
        assert result['issue_number'] == '8'
        assert result['timestamp'] == '1760714730'
    
    def test_parse_container_name_invalid_prefix(self):
        """Test parsing container name with invalid prefix"""
        recovery = AgentContainerRecovery()
        
        result = recovery.parse_container_name("invalid-container-name")
        
        assert result is None
    
    def test_parse_container_name_invalid_format(self):
        """Test parsing container name with invalid format"""
        recovery = AgentContainerRecovery()
        
        # Missing timestamp
        result = recovery.parse_container_name("claude-agent-project")
        assert result is None
        
        # Non-numeric issue number
        result = recovery.parse_container_name("claude-agent-project_agent_proj_board_abc_123")
        assert result is None
    
    @patch('services.agent_container_recovery.subprocess.run')
    def test_get_running_agent_containers(self, mock_run):
        """Test getting running agent containers from Docker"""
        recovery = AgentContainerRecovery()
        
        # Mock Docker ps output
        mock_run.return_value = Mock(
            returncode=0,
            stdout='{"Names":"claude-agent-test","ID":"abc123","Status":"Up 5 minutes","CreatedAt":"2025-10-17 16:27:28 +0000 UTC","Image":"test-image"}\n'
        )
        
        containers = recovery.get_running_agent_containers()
        
        assert len(containers) == 1
        assert containers[0]['name'] == 'claude-agent-test'
        assert containers[0]['id'] == 'abc123'
        assert containers[0]['status'] == 'Up 5 minutes'
    
    @patch('services.agent_container_recovery.subprocess.run')
    def test_get_running_agent_containers_empty(self, mock_run):
        """Test getting running agent containers when none exist"""
        recovery = AgentContainerRecovery()
        
        # Mock empty Docker ps output
        mock_run.return_value = Mock(returncode=0, stdout='')
        
        containers = recovery.get_running_agent_containers()
        
        assert len(containers) == 0
    
    @patch('services.agent_container_recovery.subprocess.run')
    def test_get_running_agent_containers_error(self, mock_run):
        """Test handling Docker ps errors"""
        recovery = AgentContainerRecovery()
        
        # Mock Docker ps failure
        mock_run.return_value = Mock(
            returncode=1,
            stderr='Docker not available'
        )
        
        containers = recovery.get_running_agent_containers()
        
        assert len(containers) == 0
    
    def test_check_execution_history_in_progress(self):
        """Test checking execution history for in_progress state"""
        recovery = AgentContainerRecovery()
        
        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            # Mock execution history file
            mock_file = MagicMock()
            mock_tracker._get_history_file.return_value = mock_file
            mock_file.exists.return_value = True
            
            with patch('builtins.open', create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = """
execution_history:
  - column: Testing
    agent: senior_software_engineer
    timestamp: '2025-10-17T16:27:28Z'
    outcome: in_progress
"""
                
                result = recovery.check_execution_history('test_project', 8)
                
                assert result is not None
                assert result['outcome'] == 'in_progress'
                assert result['agent'] == 'senior_software_engineer'
    
    @patch('services.agent_container_recovery.subprocess.run')
    def test_kill_container(self, mock_run):
        """Test killing a Docker container"""
        recovery = AgentContainerRecovery()
        
        mock_run.return_value = Mock(returncode=0)
        
        recovery.kill_container('test-container', 'abc123')
        
        mock_run.assert_called_once_with(
            ['docker', 'kill', 'abc123'],
            capture_output=True,
            timeout=10
        )
    
    def test_cleanup_execution_state(self):
        """Test cleaning up execution state"""
        recovery = AgentContainerRecovery()
        
        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            recovery.cleanup_execution_state(
                'test_project', 8, 'senior_software_engineer', 'orchestrator_restart'
            )
            
            mock_tracker.record_execution_outcome.assert_called_once_with(
                issue_number=8,
                column='unknown',
                agent='senior_software_engineer',
                outcome='failed',
                project_name='test_project',
                error='orchestrator_restart'
            )
