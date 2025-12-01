import unittest
from unittest.mock import MagicMock, patch, call
import sys
import os
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from services.pipeline_lock_manager import PipelineLockManager, PipelineLock

class TestPipelineLockManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_try_acquire_lock_success(self):
        # Setup Redis pipeline mock
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline
        
        # First pass: exists(key) -> False (lock doesn't exist)
        # But we changed logic to use transaction callback.
        # The transaction method calls the callback.
        
        # We need to mock the transaction behavior.
        # Since transaction executes a callable, we can just invoke it manually or trust the logic?
        # Mocking redis transaction is hard.
        # Let's mock the transaction method to call our callback.
        
        def side_effect_transaction(func, *keys, **kwargs):
            # Create a mock pipe that simulates the state we want
            mock_pipe = MagicMock()
            
            # Scenario: Lock does not exist
            mock_pipe.hgetall.return_value = {} 
            
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        success, reason = self.manager.try_acquire_lock("proj", "board", 123)
        
        self.assertTrue(success)
        self.assertEqual(reason, "lock_acquired")

    def test_try_acquire_lock_already_held(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock exists and held by us
            mock_pipe.hgetall.return_value = {
                'lock_status': 'locked',
                'locked_by_issue': '123'
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        success, reason = self.manager.try_acquire_lock("proj", "board", 123)
        
        self.assertTrue(success)
        self.assertEqual(reason, "already_holds_lock")

    def test_try_acquire_lock_held_by_other(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock exists and held by OTHER
            mock_pipe.hgetall.return_value = {
                'lock_status': 'locked',
                'locked_by_issue': '456',
                'lock_acquired_at': datetime.now(timezone.utc).isoformat()
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        success, reason = self.manager.try_acquire_lock("proj", "board", 123)
        
        self.assertFalse(success)
        self.assertEqual(reason, "locked_by_issue_456")

    def test_release_lock_success(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock held by us
            mock_pipe.hgetall.return_value = {
                'locked_by_issue': '123'
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        result = self.manager.release_lock("proj", "board", 123)
        self.assertTrue(result)

    def test_release_lock_held_by_other(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock held by OTHER
            mock_pipe.hgetall.return_value = {
                'locked_by_issue': '456'
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        result = self.manager.release_lock("proj", "board", 123)
        self.assertFalse(result)

if __name__ == '__main__':
    unittest.main()
