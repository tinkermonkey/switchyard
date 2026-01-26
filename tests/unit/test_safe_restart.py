import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Mock redis module before importing scripts.safe_restart
sys.modules['redis'] = MagicMock()

# Mock services modules
sys.modules['services'] = MagicMock()
sys.modules['services.agent_container_recovery'] = MagicMock()

from scripts.safe_restart import check_safety

class TestSafeRestart(unittest.TestCase):

    @patch('scripts.safe_restart.get_agent_container_recovery')
    @patch('scripts.safe_restart.redis.Redis')
    def test_check_safety_safe(self, mock_redis, mock_recovery_getter):
        # Setup mocks for a SAFE condition
        mock_recovery = MagicMock()
        mock_recovery.get_running_repair_cycle_containers.return_value = [] # No running repair cycles
        mock_recovery_getter.return_value = mock_recovery

        # Test
        is_safe = check_safety("test-container")
        self.assertTrue(is_safe)

    @patch('scripts.safe_restart.get_agent_container_recovery')
    @patch('scripts.safe_restart.redis.Redis')
    def test_check_safety_unsafe_repair_cycle(self, mock_redis, mock_recovery_getter):
        # Setup mocks for UNSAFE condition (active repair cycle)
        mock_recovery = MagicMock()
        mock_recovery.get_running_repair_cycle_containers.return_value = [
            {'name': 'repair-cycle-container-1'}
        ]
        mock_recovery.parse_repair_cycle_container_name.return_value = {'project': 'test-project'}
        mock_recovery_getter.return_value = mock_recovery

        # Test
        is_safe = check_safety("repair-cycle-container-1")
        self.assertFalse(is_safe)

    @patch('scripts.safe_restart.get_agent_container_recovery')
    @patch('scripts.safe_restart.ClaudeInvestigationQueue')
    @patch('scripts.safe_restart.ClaudeReportManager')
    @patch('scripts.safe_restart.redis.Redis')
    def test_check_safety_unsafe_investigation(self, mock_redis, mock_report_manager_cls, mock_queue_cls, mock_recovery_getter):
        # Setup mocks for UNSAFE condition (active investigation)
        mock_recovery = MagicMock()
        mock_recovery.get_running_repair_cycle_containers.return_value = []
        mock_recovery_getter.return_value = mock_recovery

        mock_queue = MagicMock()
        mock_queue.get_active.return_value = ['fingerprint-123']
        mock_queue_cls.return_value = mock_queue
        
        mock_report_manager = MagicMock()
        mock_report_manager.get_investigation_summary.return_value = {'project': 'test-project'}
        mock_report_manager_cls.return_value = mock_report_manager

        # Test
        is_safe = check_safety("test-project-container")
        self.assertFalse(is_safe)

if __name__ == '__main__':
    unittest.main()
