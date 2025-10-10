"""
Integration tests for Claude Code with mocked CLI

Tests the event emission and monitoring flow without requiring actual Claude CLI.
These tests verify that the orchestrator correctly:
1. Constructs and emits monitoring events
2. Handles stream events from Claude Code
3. Publishes events to Redis for observability
4. Collects and returns response text

These tests can run in CI/CD without Claude CLI installed.
"""

import pytest
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, AsyncMock, call
from datetime import datetime

from claude.claude_integration import run_claude_code
from monitoring.observability import ObservabilityManager, EventType


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "README.md").write_text("# Test Project\n")
        (workspace / "test.py").write_text("print('test')")
        yield workspace


@pytest.fixture
def mock_redis():
    """Create mock Redis client that captures events"""
    client = Mock()
    client.ping = Mock(return_value=True)
    client.publish = Mock(return_value=1)
    client.xadd = Mock(return_value=b'12345-0')
    client.expire = Mock(return_value=True)
    client.xlen = Mock(return_value=10)
    return client


@pytest.fixture
def obs_manager(mock_redis):
    """Create ObservabilityManager with mock Redis"""
    obs = ObservabilityManager(redis_client=mock_redis, enabled=True)
    obs.redis = mock_redis
    return obs


@pytest.fixture
def stream_events():
    """Fixture to collect stream events"""
    events = []
    
    def callback(event):
        events.append(event)
    
    callback.events = events
    return callback


@pytest.fixture
def mock_claude_process():
    """
    Mock subprocess.Popen for Claude CLI that generates realistic stream events
    
    Simulates Claude Code's stream-json output format
    """
    
    def create_mock_process(stdout_events, returncode=0):
        """
        Create a mock process with specified stdout events
        
        Args:
            stdout_events: List of dicts to be emitted as JSON lines
            returncode: Process exit code (0 = success)
        """
        # Convert events to JSON lines
        stdout_lines = [json.dumps(event) + '\n' for event in stdout_events]
        
        # Create mock process
        process = Mock()
        process.returncode = returncode
        process.stdout = Mock()
        process.stderr = Mock()
        process.stdin = Mock()
        
        # Make stdout.readline() return events one by one
        process.stdout.readline = Mock(side_effect=stdout_lines + [''])
        
        # Make stderr.readline() return nothing
        process.stderr.readline = Mock(return_value='')
        
        # Mock wait() to set returncode
        def wait_impl(timeout=None):
            process.returncode = returncode
        
        process.wait = Mock(side_effect=wait_impl)
        
        return process
    
    return create_mock_process


class TestClaudeCodeEventEmission:
    """Test that monitoring events are correctly emitted"""
    
    @pytest.mark.asyncio
    async def test_prompt_constructed_event(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that prompt_constructed event is emitted with correct data"""
        
        # Mock Claude CLI output
        claude_events = [
            {'type': 'status', 'status': 'starting'},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Hello, World!'}]}, 'session_id': 'test-session-123'},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                # Mock 'which claude' to return success
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_001',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                prompt = "Say hello"
                
                await run_claude_code(prompt, context)
        
        # Verify prompt_constructed event was published
        redis_client = obs_manager.redis
        assert redis_client.publish.called
        
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        prompt_events = [e for e in events if e['event_type'] == 'prompt_constructed']
        
        assert len(prompt_events) > 0
        
        event = prompt_events[0]
        assert event['agent'] == 'test_agent'
        assert event['task_id'] == 'test_001'
        assert event['project'] == 'test_project'
        assert event['data']['prompt'] == prompt
        assert event['data']['prompt_length'] == len(prompt)
        assert 'timestamp' in event
    
    @pytest.mark.asyncio
    async def test_claude_call_started_event(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that claude_api_call_started event is emitted"""
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Test'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_002',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Check for started event
        redis_client = obs_manager.redis
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        started_events = [e for e in events if e['event_type'] == 'claude_api_call_started']
        
        assert len(started_events) > 0
        
        event = started_events[0]
        assert event['data']['model'] == 'claude-sonnet-4-5-20250929'
        assert 'start_time' in event['data']
    
    @pytest.mark.asyncio
    async def test_claude_call_completed_event(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that claude_api_call_completed event includes token usage"""
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Response'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 50, 'output_tokens': 25}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_003',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Check for completed event
        redis_client = obs_manager.redis
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        completed_events = [e for e in events if e['event_type'] == 'claude_api_call_completed']
        
        assert len(completed_events) > 0
        
        event = completed_events[0]
        data = event['data']
        
        assert 'duration_ms' in data
        assert 'input_tokens' in data
        assert 'output_tokens' in data
        assert 'total_tokens' in data
        
        assert data['input_tokens'] == 50
        assert data['output_tokens'] == 25
        assert data['total_tokens'] == 75
        assert data['duration_ms'] > 0


class TestClaudeCodeStreamEvents:
    """Test that stream events are properly captured and forwarded"""
    
    @pytest.mark.asyncio
    async def test_stream_callback_receives_events(self, temp_workspace, stream_events, mock_claude_process):
        """Test that stream callback receives all events"""
        
        claude_events = [
            {'type': 'status', 'status': 'starting'},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Hello'}]}, 'session_id': 'test-123'},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'World'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_004',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'stream_callback': stream_events,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Verify stream callback received all events
        assert len(stream_events.events) == 4
        
        event_types = [e['type'] for e in stream_events.events]
        assert 'status' in event_types
        assert 'assistant' in event_types
        assert 'usage' in event_types
    
    @pytest.mark.asyncio
    async def test_stream_events_capture_session_id(self, temp_workspace, stream_events, mock_claude_process):
        """Test that session_id is captured from stream events"""
        
        test_session_id = 'session-abc-123'
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Hi'}]}, 'session_id': test_session_id},
            {'type': 'usage', 'usage': {'input_tokens': 5, 'output_tokens': 3}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_005',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'stream_callback': stream_events,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Verify session_id was captured in context
        assert 'claude_session_id' in context
        assert context['claude_session_id'] == test_session_id
        
        # Verify session_id is in stream events
        session_events = [e for e in stream_events.events if 'session_id' in e]
        assert len(session_events) > 0
        assert session_events[0]['session_id'] == test_session_id
    
    @pytest.mark.asyncio
    async def test_response_text_collected_from_assistant_events(self, temp_workspace, mock_claude_process):
        """Test that response text is properly collected from assistant events"""
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Part 1 '}]}, 'session_id': 'test-123'},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Part 2 '}]}, 'session_id': 'test-123'},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Part 3'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 15}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_006',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                result = await run_claude_code("test", context)
        
        # Verify response text is concatenated correctly
        assert result == 'Part 1 Part 2 Part 3'
    
    @pytest.mark.asyncio
    async def test_token_usage_tracked_from_stream(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that token usage is extracted from stream events"""
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Response'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 100, 'output_tokens': 50}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_007',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Verify token counts in completed event
        redis_client = obs_manager.redis
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        completed_events = [e for e in events if e['event_type'] == 'claude_api_call_completed']
        
        assert len(completed_events) > 0
        data = completed_events[0]['data']
        
        assert data['input_tokens'] == 100
        assert data['output_tokens'] == 50


class TestClaudeCodeRedisPublishing:
    """Test that events are correctly published to Redis"""
    
    @pytest.mark.asyncio
    async def test_events_published_to_pubsub_channel(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that events are published to Redis pub/sub channel"""
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Test'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_008',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Verify publish was called
        redis_client = obs_manager.redis
        assert redis_client.publish.called
        
        # Verify correct channel
        publish_calls = redis_client.publish.call_args_list
        channels = [call[0][0] for call in publish_calls]
        
        assert 'orchestrator:agent_events' in channels
    
    @pytest.mark.asyncio
    async def test_events_added_to_redis_stream(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that events are added to Redis stream for history"""
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Test'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_009',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Verify xadd was called
        redis_client = obs_manager.redis
        assert redis_client.xadd.called
        
        # Verify correct stream key
        xadd_calls = redis_client.xadd.call_args_list
        stream_keys = [call[0][0] for call in xadd_calls]
        
        assert 'orchestrator:event_stream' in stream_keys
    
    @pytest.mark.asyncio
    async def test_stream_ttl_set(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that TTL is set on Redis stream"""
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Test'}]}, 'session_id': 'test-123'},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_010',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("test", context)
        
        # Verify expire was called
        redis_client = obs_manager.redis
        assert redis_client.expire.called


class TestClaudeCodeErrorScenarios:
    """Test error handling and error events"""
    
    @pytest.mark.asyncio
    async def test_cli_failure_raises_exception(self, temp_workspace, obs_manager, mock_claude_process):
        """Test that CLI failure raises exception"""
        
        # Mock process that fails
        claude_events = []
        mock_process = mock_claude_process(claude_events, returncode=1)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_011',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'observability': obs_manager,
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                with pytest.raises(Exception, match="Claude CLI failed"):
                    await run_claude_code("test", context)
    
    @pytest.mark.asyncio
    async def test_error_events_in_stream(self, temp_workspace, stream_events, mock_claude_process):
        """Test that error events from Claude stream are captured"""
        
        claude_events = [
            {'type': 'error', 'error': 'Invalid model specified', 'message': 'Model not found'}
        ]
        
        mock_process = mock_claude_process(claude_events, returncode=1)
        
        with patch('subprocess.Popen', return_value=mock_process):
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_012',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'stream_callback': stream_events,
                    'claude_model': 'invalid-model'
                }
                
                with pytest.raises(Exception):
                    await run_claude_code("test", context)
        
        # Verify error event was captured in stream
        error_events = [e for e in stream_events.events if e.get('type') == 'error']
        assert len(error_events) > 0
        assert 'error' in error_events[0]


class TestClaudeCodeSessionContinuity:
    """Test multi-turn conversation session management"""
    
    @pytest.mark.asyncio
    async def test_session_resume_with_existing_id(self, temp_workspace, mock_claude_process):
        """Test that existing session_id is passed to Claude CLI"""
        
        existing_session_id = 'existing-session-xyz'
        
        claude_events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Response'}]}, 'session_id': existing_session_id},
            {'type': 'usage', 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        ]
        
        mock_process = mock_claude_process(claude_events)
        
        with patch('subprocess.Popen', return_value=mock_process) as mock_popen:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout='/usr/local/bin/claude')
                
                context = {
                    'agent': 'test_agent',
                    'task_id': 'test_013',
                    'project': 'test_project',
                    'work_dir': str(temp_workspace),
                    'use_docker': False,
                    'claude_session_id': existing_session_id,  # Existing session
                    'claude_model': 'claude-sonnet-4-5-20250929'
                }
                
                await run_claude_code("continue conversation", context)
        
        # Verify --resume flag was passed
        popen_call = mock_popen.call_args
        cmd = popen_call[0][0]
        
        assert '--resume' in cmd
        resume_index = cmd.index('--resume')
        assert cmd[resume_index + 1] == existing_session_id


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
