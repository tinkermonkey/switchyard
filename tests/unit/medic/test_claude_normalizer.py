"""
Unit tests for Claude Code Smart Normalizer
"""

import asyncio
import json
import pytest
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from services.medic.claude_normalizer import ClaudeCodeNormalizer


class TestClaudeCodeNormalizer:
    """Test suite for ClaudeCodeNormalizer"""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis client"""
        redis = Mock()
        redis.get = Mock(return_value=None)
        redis.setex = Mock()
        redis.lpush = Mock()
        redis.ltrim = Mock()
        return redis

    @pytest.fixture
    def normalizer(self, mock_redis):
        """Create normalizer instance with mocked Redis"""
        with patch('services.medic.claude_normalizer.subprocess.run') as mock_run:
            # Mock Claude CLI availability check
            mock_run.return_value = Mock(returncode=0, stdout="claude version 1.0")
            normalizer = ClaudeCodeNormalizer(mock_redis)
            return normalizer

    @pytest.fixture
    def normalizer_no_claude(self, mock_redis):
        """Create normalizer instance without Claude CLI"""
        with patch('services.medic.claude_normalizer.subprocess.run') as mock_run:
            # Mock Claude CLI not available
            mock_run.side_effect = FileNotFoundError()
            normalizer = ClaudeCodeNormalizer(mock_redis)
            return normalizer

    def test_init_with_claude_available(self, mock_redis):
        """Test initialization when Claude CLI is available"""
        with patch('services.medic.claude_normalizer.subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="claude version 1.0")
            normalizer = ClaudeCodeNormalizer(mock_redis)

            assert normalizer.claude_available is True
            assert normalizer.redis == mock_redis
            assert normalizer.cache_ttl == 86400

    def test_init_without_claude_available(self, mock_redis):
        """Test initialization when Claude CLI is not available"""
        with patch('services.medic.claude_normalizer.subprocess.run') as mock_run:
            mock_run.side_effect = FileNotFoundError()
            normalizer = ClaudeCodeNormalizer(mock_redis)

            assert normalizer.claude_available is False

    @pytest.mark.asyncio
    async def test_normalize_cache_hit(self, normalizer, mock_redis):
        """Test that cache hits return cached results without calling Claude"""
        cached_result = {
            "normalized": "Elasticsearch connection failed for {count} times",
            "confidence": 0.95,
            "suggested_pattern": None,
            "reasoning": "Cached result",
            "variable_parts": ["count"]
        }

        mock_redis.get.return_value = json.dumps(cached_result)

        result = await normalizer.normalize(
            "Elasticsearch connection failed for 1000 times",
            {"container": "orchestrator", "level": "ERROR"}
        )

        assert result == cached_result
        mock_redis.get.assert_called_once()
        # Should not call setex since it was a cache hit
        mock_redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_normalize_without_claude(self, normalizer_no_claude, mock_redis):
        """Test normalization falls back when Claude CLI is not available"""
        message = "Elasticsearch connection failed for 1000 times"

        result = await normalizer_no_claude.normalize(
            message,
            {"container": "orchestrator", "level": "ERROR"}
        )

        assert result["normalized"] == message  # Fallback to original
        assert result["confidence"] == 0.5
        assert result["reasoning"] == "Claude CLI not available"

    @pytest.mark.asyncio
    async def test_normalize_with_claude_success(self, normalizer, mock_redis):
        """Test successful Claude normalization"""
        mock_redis.get.return_value = None  # Cache miss

        claude_response = {
            "normalized": "Elasticsearch connection failed for {count} times",
            "confidence": 0.95,
            "variable_parts": ["count"],
            "suggested_regex": r"Elasticsearch connection failed for \d+ times",
            "reasoning": "Normalized counter value"
        }

        # Mock subprocess for Claude call
        with patch('services.medic.claude_normalizer.asyncio.create_subprocess_exec') as mock_exec:
            # Mock process
            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(
                json.dumps(claude_response).encode(),
                b""
            ))
            mock_exec.return_value = mock_process

            # Mock tempfile
            with patch('services.medic.claude_normalizer.tempfile.NamedTemporaryFile') as mock_temp:
                mock_temp.return_value.__enter__.return_value.name = '/tmp/test.txt'

                # Mock os.path.exists and os.unlink
                with patch('services.medic.claude_normalizer.os.path.exists', return_value=True):
                    with patch('services.medic.claude_normalizer.os.unlink'):
                        result = await normalizer.normalize(
                            "Elasticsearch connection failed for 1000 times",
                            {"container": "orchestrator", "level": "ERROR"}
                        )

        assert result["normalized"] == "Elasticsearch connection failed for {count} times"
        assert result["confidence"] == 0.95
        assert result["variable_parts"] == ["count"]
        assert "suggested_regex" in result

        # Verify cache was written
        mock_redis.setex.assert_called_once()
        cache_key = mock_redis.setex.call_args[0][0]
        assert cache_key.startswith("medic:claude_norm:")

    @pytest.mark.asyncio
    async def test_normalize_claude_timeout(self, normalizer, mock_redis):
        """Test that Claude call timeout returns fallback"""
        mock_redis.get.return_value = None  # Cache miss

        with patch('services.medic.claude_normalizer.asyncio.create_subprocess_exec') as mock_exec:
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_process.kill = AsyncMock()
            mock_process.wait = AsyncMock()
            mock_exec.return_value = mock_process

            with patch('services.medic.claude_normalizer.tempfile.NamedTemporaryFile') as mock_temp:
                mock_temp.return_value.__enter__.return_value.name = '/tmp/test.txt'

                with patch('services.medic.claude_normalizer.os.path.exists', return_value=True):
                    with patch('services.medic.claude_normalizer.os.unlink'):
                        result = await normalizer.normalize(
                            "Some error message",
                            {"container": "test"}
                        )

        assert result["confidence"] == 0.5
        assert result["reasoning"] == "Timeout"
        assert result["normalized"] == "Some error message"

    @pytest.mark.asyncio
    async def test_normalize_claude_json_parse_error(self, normalizer, mock_redis):
        """Test handling of invalid JSON from Claude"""
        mock_redis.get.return_value = None  # Cache miss

        with patch('services.medic.claude_normalizer.asyncio.create_subprocess_exec') as mock_exec:
            mock_process = AsyncMock()
            mock_process.returncode = 0
            # Invalid JSON response
            mock_process.communicate = AsyncMock(return_value=(
                b"This is not valid JSON",
                b""
            ))
            mock_exec.return_value = mock_process

            with patch('services.medic.claude_normalizer.tempfile.NamedTemporaryFile') as mock_temp:
                mock_temp.return_value.__enter__.return_value.name = '/tmp/test.txt'

                with patch('services.medic.claude_normalizer.os.path.exists', return_value=True):
                    with patch('services.medic.claude_normalizer.os.unlink'):
                        result = await normalizer.normalize(
                            "Some error message",
                            {"container": "test"}
                        )

        assert result["confidence"] == 0.5
        assert "JSON parse error" in result["reasoning"]
        assert result["normalized"] == "Some error message"

    def test_extract_json_from_markdown(self, normalizer):
        """Test extracting JSON from markdown code blocks"""
        # Test with json code block
        output = '''Here is the normalized pattern:
```json
{"normalized": "test {count}", "confidence": 0.9, "reasoning": "test"}
```
'''
        result = normalizer._extract_json_response(output)
        assert result is not None
        assert result["normalized"] == "test {count}"

        # Test with plain code block
        output = '''
```
{"normalized": "test {count}", "confidence": 0.9, "reasoning": "test"}
```
'''
        result = normalizer._extract_json_response(output)
        assert result is not None

        # Test with embedded JSON
        output = '''Some text before {"normalized": "test", "confidence": 0.9, "reasoning": "x"} and after'''
        result = normalizer._extract_json_response(output)
        assert result is not None
        assert result["normalized"] == "test"

    def test_validate_response(self, normalizer):
        """Test response validation"""
        # Valid response
        valid = {
            "normalized": "test message",
            "confidence": 0.95,
            "reasoning": "test"
        }
        assert normalizer._validate_response(valid) is True

        # Missing required field
        invalid = {
            "normalized": "test message",
            "confidence": 0.95
            # Missing reasoning
        }
        assert normalizer._validate_response(invalid) is False

    def test_fallback_response(self, normalizer):
        """Test fallback response generation"""
        message = "Test error message"
        reason = "Test reason"

        result = normalizer._fallback_response(message, reason)

        assert result["normalized"] == message
        assert result["confidence"] == 0.5
        assert result["suggested_pattern"] is None
        assert result["reasoning"] == reason

    def test_store_pattern_suggestion(self, normalizer, mock_redis):
        """Test storing pattern suggestions in Redis"""
        pattern = r"test pattern \d+"
        example = "test example 123"
        reasoning = "Test reasoning"

        normalizer.store_pattern_suggestion(pattern, example, reasoning)

        # Verify Redis calls
        mock_redis.lpush.assert_called_once()
        mock_redis.ltrim.assert_called_once_with("medic:suggested_patterns", 0, 99)

        # Verify stored data structure
        call_args = mock_redis.lpush.call_args[0]
        assert call_args[0] == "medic:suggested_patterns"

        stored_data = json.loads(call_args[1])
        assert stored_data["pattern"] == pattern
        assert stored_data["example"] == example
        assert stored_data["reasoning"] == reasoning
        assert "timestamp" in stored_data

    def test_store_pattern_suggestion_redis_error(self, normalizer, mock_redis):
        """Test that Redis errors in pattern storage don't crash"""
        mock_redis.lpush.side_effect = Exception("Redis error")

        # Should not raise exception
        normalizer.store_pattern_suggestion("pattern", "example", "reason")

    def test_build_normalization_prompt(self, normalizer):
        """Test prompt building for Claude"""
        message = "Elasticsearch connection failed for 1000 times"
        context = {
            "container": "orchestrator",
            "level": "ERROR",
            "timestamp": "2025-01-05T12:00:00Z"
        }

        prompt = normalizer._build_normalization_prompt(message, context)

        # Verify prompt contains key elements
        assert message in prompt
        assert "orchestrator" in prompt
        assert "ERROR" in prompt
        assert "variable parts" in prompt.lower()
        assert "json" in prompt.lower()
        assert "{count}" in prompt  # Example placeholder

    @pytest.mark.asyncio
    async def test_normalize_redis_cache_error_doesnt_crash(self, normalizer, mock_redis):
        """Test that Redis errors don't crash normalization"""
        mock_redis.get.side_effect = Exception("Redis error")
        mock_redis.setex.side_effect = Exception("Redis error")

        # Should fall back to no Claude available
        result = await normalizer.normalize(
            "Test message",
            {"container": "test"}
        )

        # Should still return a result (fallback)
        assert result is not None
        assert "normalized" in result
