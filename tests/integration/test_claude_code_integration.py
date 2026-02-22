"""
Integration tests for Claude Code execution

Tests that Claude Code executes correctly and generates expected monitoring events
and live log events for observability.

These tests use simple prompts like "hello world" and "list files" to verify:
1. Claude Code CLI is invoked correctly
2. Monitoring events are emitted during execution
3. Live streaming events are captured and published
4. Response text is properly collected from stream
"""

import pytest
import asyncio
import json
import os
import tempfile
import logging
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from datetime import datetime

from claude.claude_integration import run_claude_code
from claude.docker_runner import docker_runner
from monitoring.observability import ObservabilityManager, EventType

logger = logging.getLogger(__name__)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        # Create a few test files
        (workspace / "README.md").write_text("# Test Project\n\nThis is a test.")
        (workspace / "hello.py").write_text("print('Hello, World!')")
        yield workspace


@pytest.fixture
def docker_workspace():
    """
    Create a workspace directory for Docker tests.

    Uses /workspace/test_project if running in orchestrator container,
    otherwise creates a test directory in /tmp with proper ownership for Docker.
    """
    # Check if we're in the orchestrator container with /workspace
    workspace_root = Path('/workspace')
    if workspace_root.exists() and workspace_root.is_dir():
        # Running in container - use real workspace
        test_project_dir = workspace_root / 'test_project'
        test_project_dir.mkdir(exist_ok=True)

        # Create test files
        (test_project_dir / "README.md").write_text("# Test Project\n\nThis is a test.")
        (test_project_dir / "hello.py").write_text("print('Hello, World!')")

        yield test_project_dir

        # Cleanup
        import shutil
        if test_project_dir.exists():
            try:
                shutil.rmtree(test_project_dir)
            except Exception as e:
                logger.warning(f"Could not clean up test project: {e}")
    else:
        # Not in container - use the orchestrator workspace if available
        # Otherwise, skip Docker tests as they require the orchestrator environment
        import shutil
        import uuid

        # Check if we have access to the orchestrator workspace area
        orchestrator_workspace = Path.home().parent / 'austinsand' / 'workspace' / 'orchestrator'
        if not orchestrator_workspace.exists():
            # Try alternative location
            orchestrator_workspace = Path('/home/austinsand/workspace/orchestrator')

        if orchestrator_workspace.exists() and orchestrator_workspace.is_dir():
            # Use a test directory within the orchestrator workspace
            test_id = str(uuid.uuid4())[:8]
            test_dir = orchestrator_workspace / f'test_project_{test_id}'

            try:
                # Create directory with current user ownership
                test_dir.mkdir(parents=True, exist_ok=True)

                # Create test files
                (test_dir / "README.md").write_text("# Test Project\n\nThis is a test.")
                (test_dir / "hello.py").write_text("print('Hello, World!')")

                yield test_dir

            finally:
                # Cleanup
                if test_dir.exists():
                    try:
                        shutil.rmtree(test_dir)
                    except Exception as e:
                        logger.warning(f"Could not clean up test directory: {e}")
        else:
            # Can't find suitable workspace - skip Docker tests
            pytest.skip("Docker tests require orchestrator workspace environment")


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
    """Fixture to collect stream events from Claude Code"""
    events = []
    
    def callback(event):
        """Callback that captures stream events"""
        events.append(event)
    
    # Attach events list to callback for easy access
    callback.events = events
    return callback


class TestClaudeCodeLocalExecution:
    """Test Claude Code execution in local mode (no Docker)"""
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_hello_world_prompt(self, temp_workspace, obs_manager, stream_events):
        """Test simple hello world prompt generates correct events"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_hello_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,  # Force local execution
            'observability': obs_manager,
            'stream_callback': stream_events,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "Say 'Hello, World!' and nothing else."
        
        # Execute Claude Code
        result = await run_claude_code(prompt, context)
        
        # Verify we got a result
        assert result is not None
        assert len(result) > 0
        assert "hello" in result.lower() or "world" in result.lower()
        
        # Verify monitoring events were published
        redis_client = obs_manager.redis
        assert redis_client.publish.called
        
        # Check for expected event types
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        event_types = [e['event_type'] for e in events]
        
        # Should have: prompt_constructed, claude_api_call_started, claude_api_call_completed
        assert 'prompt_constructed' in event_types
        assert 'claude_api_call_started' in event_types
        assert 'claude_api_call_completed' in event_types
        
        # Verify prompt_constructed event
        prompt_events = [e for e in events if e['event_type'] == 'prompt_constructed']
        assert len(prompt_events) > 0
        prompt_event = prompt_events[0]
        assert prompt_event['data']['prompt_length'] == len(prompt)
        assert 'prompt' in prompt_event['data']
        
        # Verify claude_api_call_completed has token usage
        completion_events = [e for e in events if e['event_type'] == 'claude_api_call_completed']
        assert len(completion_events) > 0
        completion_event = completion_events[0]
        assert 'input_tokens' in completion_event['data']
        assert 'output_tokens' in completion_event['data']
        assert completion_event['data']['input_tokens'] > 0
        assert completion_event['data']['output_tokens'] > 0
        
        # Verify stream events were captured
        assert len(stream_events.events) > 0
        
        # Should have various stream event types
        stream_event_types = {e.get('type') for e in stream_events.events if 'type' in e}
        
        # Typical events: status, assistant, result, usage
        assert 'assistant' in stream_event_types or 'result' in stream_event_types
        
        # Verify session_id was captured (for continuity)
        session_events = [e for e in stream_events.events if 'session_id' in e]
        assert len(session_events) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_list_files_prompt(self, temp_workspace, obs_manager, stream_events):
        """Test file listing prompt generates correct events"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_list_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'observability': obs_manager,
            'stream_callback': stream_events,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "List all files in the current directory. Just show the filenames."
        
        # Execute Claude Code
        result = await run_claude_code(prompt, context)
        
        # Verify we got a result with expected files
        assert result is not None
        assert len(result) > 0
        
        # Should mention the test files we created
        assert "README.md" in result or "hello.py" in result
        
        # Verify stream events captured tool usage (file reading)
        # Claude Code may use file reading tools
        tool_events = [e for e in stream_events.events 
                      if e.get('type') == 'tool_use' or 'tool' in str(e.get('type', ''))]
        
        # Note: Tool events may not always be present for simple file listing
        # But stream events should definitely be present
        assert len(stream_events.events) > 0
        
        # Verify monitoring events
        redis_client = obs_manager.redis
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        
        # Check completion event has duration
        completion_events = [e for e in events if e['event_type'] == 'claude_api_call_completed']
        assert len(completion_events) > 0
        completion_event = completion_events[0]
        assert 'duration_ms' in completion_event['data']
        assert completion_event['data']['duration_ms'] > 0
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_session_continuity(self, temp_workspace, obs_manager, stream_events):
        """Test that session_id is captured for multi-turn conversations"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_session_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'observability': obs_manager,
            'stream_callback': stream_events,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        # First turn
        prompt1 = "What is 2 + 2?"
        result1 = await run_claude_code(prompt1, context)
        
        assert result1 is not None
        
        # Check if session_id was stored in context
        assert 'claude_session_id' in context
        session_id = context['claude_session_id']
        assert session_id is not None
        assert len(session_id) > 0
        
        # Verify session_id appeared in stream events
        session_events = [e for e in stream_events.events if 'session_id' in e]
        assert len(session_events) > 0
        assert session_events[0]['session_id'] == session_id


class TestClaudeCodeDockerExecution:
    """Test Claude Code execution in Docker containers"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("docker info > /dev/null 2>&1") != 0,
        reason="Docker not available"
    )
    @pytest.mark.skipif(
        not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') and not os.environ.get('ANTHROPIC_API_KEY'),
        reason="Claude API key not configured (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY required)"
    )
    async def test_docker_hello_world(self, docker_workspace, obs_manager, stream_events):
        """Test simple prompt in Docker generates correct events"""

        # Mock workspace manager to return our temp workspace
        with patch('claude.claude_integration.workspace_manager') as mock_wm:
            mock_wm.get_project_dir.return_value = docker_workspace
            
            # Mock agent config to require Docker
            mock_agent_config = Mock()
            mock_agent_config.requires_docker = True
            mock_agent_config.filesystem_write_allowed = True
            
            context = {
                'agent': 'test_agent',
                'task_id': 'test_docker_hello_001',
                'project': 'test_project',
                'use_docker': True,
                'agent_config': mock_agent_config,
                'observability': obs_manager,
                'stream_callback': stream_events,
                'claude_model': 'claude-sonnet-4-5-20250929'
            }
            
            prompt = "Echo 'Hello from Docker!' and nothing else."

            # Execute Claude Code in Docker
            result = await run_claude_code(prompt, context)

            # Verify execution completed successfully
            # Note: Result might be empty if Claude decides not to output text
            # (e.g., if the command was executed but no text response was generated)
            assert result is not None
            
            # Verify monitoring events
            redis_client = obs_manager.redis
            assert redis_client.publish.called
            
            publish_calls = redis_client.publish.call_args_list
            event_jsons = [call[0][1] for call in publish_calls 
                          if call[0][0] == 'orchestrator:agent_events']
            
            events = [json.loads(e) for e in event_jsons]
            event_types = [e['event_type'] for e in events]
            
            # Should have core monitoring events
            assert 'prompt_constructed' in event_types
            assert 'claude_api_call_started' in event_types
            assert 'claude_api_call_completed' in event_types
            
            # Verify stream events
            assert len(stream_events.events) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("docker info > /dev/null 2>&1") != 0,
        reason="Docker not available"
    )
    @pytest.mark.skipif(
        not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') and not os.environ.get('ANTHROPIC_API_KEY'),
        reason="Claude API key not configured (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY required)"
    )
    async def test_docker_file_operations(self, docker_workspace, obs_manager, stream_events):
        """Test file operations in Docker container"""

        with patch('claude.claude_integration.workspace_manager') as mock_wm:
            mock_wm.get_project_dir.return_value = docker_workspace
            
            mock_agent_config = Mock()
            mock_agent_config.requires_docker = True
            mock_agent_config.filesystem_write_allowed = True
            
            context = {
                'agent': 'test_agent',
                'task_id': 'test_docker_files_001',
                'project': 'test_project',
                'use_docker': True,
                'agent_config': mock_agent_config,
                'observability': obs_manager,
                'stream_callback': stream_events,
                'claude_model': 'claude-sonnet-4-5-20250929'
            }
            
            prompt = "Read the README.md file and tell me what it says."
            
            result = await run_claude_code(prompt, context)
            
            # Should mention content from README.md
            assert result is not None
            assert "test" in result.lower() or "project" in result.lower()
            
            # Verify events were published
            redis_client = obs_manager.redis
            assert redis_client.publish.called
            
            # Verify stream events include file reading
            assert len(stream_events.events) > 0


class TestClaudeCodeEventStructure:
    """Test the structure and content of monitoring events"""
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_prompt_constructed_event_structure(self, temp_workspace, obs_manager):
        """Verify prompt_constructed event has correct structure"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_event_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'observability': obs_manager,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "Test prompt for event structure"
        
        await run_claude_code(prompt, context)
        
        # Extract prompt_constructed events
        redis_client = obs_manager.redis
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        prompt_events = [e for e in events if e['event_type'] == 'prompt_constructed']
        
        assert len(prompt_events) > 0
        event = prompt_events[0]
        
        # Verify top-level structure
        assert 'timestamp' in event
        assert 'event_type' in event
        assert 'agent' in event
        assert 'task_id' in event
        assert 'project' in event
        assert 'data' in event
        
        # Verify data structure
        data = event['data']
        assert 'prompt' in data
        assert 'prompt_preview' in data
        assert 'prompt_length' in data
        assert data['prompt'] == prompt
        assert data['prompt_length'] == len(prompt)
        
        # Verify metadata
        assert event['agent'] == 'test_agent'
        assert event['task_id'] == 'test_event_001'
        assert event['project'] == 'test_project'
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_claude_call_events_structure(self, temp_workspace, obs_manager):
        """Verify claude_api_call events have correct structure"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_call_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'observability': obs_manager,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "Simple test"
        
        await run_claude_code(prompt, context)
        
        # Extract events
        redis_client = obs_manager.redis
        publish_calls = redis_client.publish.call_args_list
        event_jsons = [call[0][1] for call in publish_calls 
                      if call[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(e) for e in event_jsons]
        
        # Check started event
        started_events = [e for e in events if e['event_type'] == 'claude_api_call_started']
        assert len(started_events) > 0
        started = started_events[0]
        
        assert 'model' in started['data']
        assert started['data']['model'] == 'claude-sonnet-4-5-20250929'
        assert 'start_time' in started['data']
        
        # Check completed event
        completed_events = [e for e in events if e['event_type'] == 'claude_api_call_completed']
        assert len(completed_events) > 0
        completed = completed_events[0]
        
        data = completed['data']
        assert 'duration_ms' in data
        assert 'input_tokens' in data
        assert 'output_tokens' in data
        assert 'total_tokens' in data
        
        assert data['duration_ms'] > 0
        assert data['input_tokens'] > 0
        assert data['output_tokens'] > 0
        assert data['total_tokens'] == data['input_tokens'] + data['output_tokens'] + data.get('cache_read_tokens', 0) + data.get('cache_creation_tokens', 0)


class TestClaudeCodeStreamEvents:
    """Test live stream events from Claude Code"""
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_stream_event_types(self, temp_workspace, stream_events):
        """Verify stream events contain expected types"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_stream_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'stream_callback': stream_events,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "Count from 1 to 5"
        
        await run_claude_code(prompt, context)
        
        # Verify we got stream events
        assert len(stream_events.events) > 0
        
        # Collect event types
        event_types = {e.get('type') for e in stream_events.events if 'type' in e}
        
        # Common stream event types from Claude Code CLI
        # Actual types may vary, but we should see some of these:
        # - assistant: AI response content
        # - result: Final result
        # - usage: Token usage
        # - status: Status updates
        # - session_id: Session information
        
        # At minimum, we should have some recognizable events
        assert len(event_types) > 0

        # Log event types for debugging
        logger.info(f"Stream event types received: {event_types}")
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_stream_assistant_events(self, temp_workspace, stream_events):
        """Verify assistant events contain message content"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_assistant_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'stream_callback': stream_events,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "Say hello"
        
        await run_claude_code(prompt, context)
        
        # Find assistant events
        assistant_events = [e for e in stream_events.events if e.get('type') == 'assistant']
        
        if len(assistant_events) > 0:
            # Verify structure
            event = assistant_events[0]
            assert 'message' in event
            message = event['message']
            assert 'content' in message
            
            # Content should be array of content blocks
            content = message['content']
            assert isinstance(content, list)
            
            # Should have at least one text block
            text_blocks = [c for c in content if isinstance(c, dict) and c.get('type') == 'text']
            assert len(text_blocks) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_stream_usage_events(self, temp_workspace, stream_events):
        """Verify usage events contain token counts"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_usage_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'stream_callback': stream_events,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "Brief test"
        
        await run_claude_code(prompt, context)
        
        # Find events with usage information
        usage_events = [e for e in stream_events.events if 'usage' in e]
        
        assert len(usage_events) > 0
        
        # Verify usage structure
        event = usage_events[0]
        usage = event['usage']
        
        assert 'input_tokens' in usage
        assert 'output_tokens' in usage
        assert usage['input_tokens'] > 0
        assert usage['output_tokens'] > 0


class TestClaudeCodeErrorHandling:
    """Test error handling and error event generation"""
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_invalid_prompt_error_events(self, temp_workspace, obs_manager, stream_events):
        """Test that errors generate appropriate events"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_error_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'observability': obs_manager,
            'stream_callback': stream_events,
            'claude_model': 'invalid-model-name'  # This should cause an error
        }
        
        prompt = "Test error handling"
        
        # Should raise an exception
        with pytest.raises(Exception):
            await run_claude_code(prompt, context)
        
        # Even with error, some events may have been published
        redis_client = obs_manager.redis
        
        # At minimum, prompt_constructed should have been emitted
        publish_calls = redis_client.publish.call_args_list
        if len(publish_calls) > 0:
            event_jsons = [call[0][1] for call in publish_calls 
                          if call[0][0] == 'orchestrator:agent_events']
            
            events = [json.loads(e) for e in event_jsons]
            event_types = [e['event_type'] for e in events]
            
            # Should have at least attempted to construct prompt
            assert 'prompt_constructed' in event_types


class TestClaudeCodeRedisStreamHistory:
    """Test that events are stored in Redis stream for history"""
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.system("which claude > /dev/null 2>&1") != 0,
        reason="Claude CLI not installed"
    )
    async def test_events_stored_in_stream(self, temp_workspace, obs_manager):
        """Verify events are added to Redis stream"""
        
        context = {
            'agent': 'test_agent',
            'task_id': 'test_stream_store_001',
            'project': 'test_project',
            'work_dir': str(temp_workspace),
            'use_docker': False,
            'observability': obs_manager,
            'claude_model': 'claude-sonnet-4-5-20250929'
        }
        
        prompt = "Quick test"
        
        await run_claude_code(prompt, context)
        
        # Verify xadd was called (Redis stream)
        redis_client = obs_manager.redis
        assert redis_client.xadd.called
        
        # Verify stream key and parameters
        xadd_calls = redis_client.xadd.call_args_list
        
        # Should have multiple xadd calls for different events
        assert len(xadd_calls) > 0
        
        # Check that events were added to the correct stream
        stream_keys = [call[0][0] for call in xadd_calls]
        assert 'orchestrator:event_stream' in stream_keys
        
        # Verify TTL was set
        assert redis_client.expire.called


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "-s"])
