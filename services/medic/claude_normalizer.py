"""
AI-Powered Error Message Normalizer using Claude Code

This normalizer uses Claude Code CLI to analyze complex error messages
and generate normalized patterns when static normalizers are insufficient.

Features:
- Confidence-based invocation (only for ambiguous cases)
- Redis caching to avoid repeated API calls
- Pattern learning and suggestions for static normalizers
- Async execution to avoid blocking
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class ClaudeCodeNormalizer:
    """
    AI-powered normalizer using Claude Code CLI.

    Analyzes error messages to identify variable vs. constant parts,
    generates normalized patterns, and suggests improvements to static normalizers.
    """

    def __init__(self, redis_client, cache_ttl: int = 86400):
        """
        Initialize Claude Code normalizer.

        Args:
            redis_client: Redis client for caching
            cache_ttl: Cache TTL in seconds (default: 24 hours)
        """
        self.redis = redis_client
        self.cache_ttl = cache_ttl
        self.claude_available = self._check_claude_availability()

        if not self.claude_available:
            logger.warning("Claude Code CLI not available - AI normalization disabled")
        else:
            logger.info("Claude Code normalizer initialized successfully")

    def _check_claude_availability(self) -> bool:
        """Check if Claude Code CLI is available."""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                timeout=2,
                text=True
            )
            if result.returncode == 0:
                logger.info(f"Claude CLI available: {result.stdout.strip()}")
                return True
            else:
                logger.warning(f"Claude CLI check failed: {result.stderr}")
                return False
        except FileNotFoundError:
            logger.warning("Claude CLI not found in PATH")
            return False
        except subprocess.TimeoutExpired:
            logger.warning("Claude CLI check timed out")
            return False
        except Exception as e:
            logger.error(f"Error checking Claude CLI availability: {e}")
            return False

    async def normalize(self, message: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize error message using Claude Code analysis.

        Args:
            message: Raw error message
            context: Additional context (container, log level, etc.)

        Returns:
            {
                "normalized": "normalized message pattern",
                "confidence": 0.95,  # 0-1 confidence score
                "suggested_pattern": "regex pattern or None",
                "reasoning": "why this normalization",
                "variable_parts": ["list of what was normalized"]
            }
        """
        # Check cache first
        cache_key = f"medic:claude_norm:{hashlib.md5(message.encode()).hexdigest()}"

        try:
            cached = self.redis.get(cache_key)
            if cached:
                logger.debug(f"Cache hit for message: {message[:50]}...")
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Redis cache read failed: {e}")

        if not self.claude_available:
            return {
                "normalized": message,
                "confidence": 0.5,
                "suggested_pattern": None,
                "reasoning": "Claude CLI not available",
                "variable_parts": []
            }

        # Call Claude Code
        prompt = self._build_normalization_prompt(message, context)
        result = await self._call_claude(prompt, message)

        # Cache result
        try:
            self.redis.setex(cache_key, self.cache_ttl, json.dumps(result))
            logger.debug(f"Cached normalization result for: {message[:50]}...")
        except Exception as e:
            logger.warning(f"Redis cache write failed: {e}")

        return result

    def _build_normalization_prompt(self, message: str, context: Dict[str, Any]) -> str:
        """Build prompt for Claude to analyze the error message."""
        return f"""Analyze this error message and normalize variable parts to create a reusable error pattern.

Error Message: {message}
Container: {context.get('container', 'unknown')}
Log Level: {context.get('level', 'unknown')}
Timestamp: {context.get('timestamp', 'unknown')}

Task: Identify which parts are variable (numbers, IDs, timestamps, paths) vs. constant (error type, action, message structure).

Replace variable parts with descriptive placeholders:
- Numbers/counts: {{count}}, {{n}}, {{total}}
- IDs: {{id}}, {{uuid}}, {{task_id}}
- Timestamps: {{timestamp}}
- Paths: {{path}}, {{project}}, {{file}}
- Durations: {{duration}}
- Status codes: {{code}}
- Percentages/ratios: {{percentage}}, {{n}}/{{total}}

Examples:
Input: "Elasticsearch connection failed for 1000 times"
Output: "Elasticsearch connection failed for {{count}} times"

Input: "Task task_ba_550e8400-e29b-41d4-a716-446655440000 timed out after 30.5 seconds"
Output: "Task {{task_id}} timed out after {{duration}} seconds"

Input: "attempt 0 of 3 failed with status 503"
Output: "attempt {{n}} of {{total}} failed with status {{code}}"

Input: "/workspace/my-project/src/main.py not found"
Output: "/workspace/{{project}}/src/main.py not found"

Respond with ONLY valid JSON in this exact format (no markdown, no extra text):
{{
  "normalized": "your normalized message with placeholders",
  "confidence": 0.95,
  "variable_parts": ["count", "timestamp"],
  "suggested_regex": "optional regex pattern to match this error class",
  "reasoning": "brief explanation of what you normalized"
}}"""

    async def _call_claude(self, prompt: str, original_message: str) -> Dict[str, Any]:
        """Call Claude Code CLI to analyze message."""
        prompt_file = None

        try:
            # Write prompt to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                f.write(prompt)
                prompt_file = f.name

            # Call Claude Code CLI
            logger.debug("Calling Claude CLI for normalization...")

            process = await asyncio.create_subprocess_exec(
                "claude",
                "-p", prompt_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.warning("Claude CLI timeout after 30s")
                process.kill()
                await process.wait()
                return self._fallback_response(original_message, "Timeout")

            # Clean up temp file
            if prompt_file and os.path.exists(prompt_file):
                os.unlink(prompt_file)
                prompt_file = None

            if process.returncode == 0:
                # Parse Claude's response
                output = stdout.decode().strip()
                logger.debug(f"Claude CLI output: {output[:200]}...")

                # Try to extract JSON from the response
                response = self._extract_json_response(output)

                if response:
                    # Validate response structure
                    if self._validate_response(response):
                        logger.info(f"Successfully normalized with Claude: {original_message[:50]}...")
                        return response
                    else:
                        logger.warning(f"Invalid response structure from Claude: {response}")
                        return self._fallback_response(original_message, "Invalid response format")
                else:
                    logger.warning(f"Could not parse JSON from Claude output: {output[:200]}")
                    return self._fallback_response(original_message, "JSON parse error")
            else:
                stderr_text = stderr.decode()
                logger.warning(f"Claude CLI failed with code {process.returncode}: {stderr_text}")
                return self._fallback_response(original_message, f"CLI error: {stderr_text[:100]}")

        except Exception as e:
            logger.error(f"Claude normalization failed: {e}", exc_info=True)
            return self._fallback_response(original_message, f"Exception: {str(e)[:100]}")

        finally:
            # Ensure temp file is cleaned up
            if prompt_file and os.path.exists(prompt_file):
                try:
                    os.unlink(prompt_file)
                except Exception as e:
                    logger.warning(f"Failed to clean up temp file {prompt_file}: {e}")

    def _extract_json_response(self, output: str) -> Optional[Dict[str, Any]]:
        """Extract JSON from Claude's response, handling markdown code blocks."""
        # Try parsing as direct JSON first
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        if "```json" in output:
            start = output.find("```json") + 7
            end = output.find("```", start)
            if end > start:
                json_text = output[start:end].strip()
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    pass

        # Try extracting from plain code block
        if "```" in output:
            start = output.find("```") + 3
            end = output.find("```", start)
            if end > start:
                json_text = output[start:end].strip()
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    pass

        # Try finding JSON object in output
        start = output.find("{")
        end = output.rfind("}") + 1
        if start >= 0 and end > start:
            json_text = output[start:end]
            try:
                return json.loads(json_text)
            except json.JSONDecodeError:
                pass

        return None

    def _validate_response(self, response: Dict[str, Any]) -> bool:
        """Validate that response has required fields."""
        required_fields = ["normalized", "confidence", "reasoning"]
        return all(field in response for field in required_fields)

    def _fallback_response(self, message: str, reason: str) -> Dict[str, Any]:
        """Return fallback response when Claude call fails."""
        return {
            "normalized": message,
            "confidence": 0.5,
            "suggested_pattern": None,
            "reasoning": reason,
            "variable_parts": []
        }

    def store_pattern_suggestion(self, pattern: str, example: str, reasoning: str):
        """Store Claude's suggested pattern for review/incorporation."""
        try:
            from datetime import datetime

            suggestion = {
                "pattern": pattern,
                "example": example,
                "reasoning": reasoning,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }

            # Store in Redis list for admin review
            self.redis.lpush(
                "medic:suggested_patterns",
                json.dumps(suggestion)
            )

            # Keep last 100 suggestions
            self.redis.ltrim("medic:suggested_patterns", 0, 99)

            logger.info(f"Stored pattern suggestion: {pattern[:50]}...")

        except Exception as e:
            logger.error(f"Failed to store pattern suggestion: {e}")
