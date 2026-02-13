#!/usr/bin/env python3
"""
Unit tests for strategy generation with Claude Code CLI
"""

import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.generate_strategy import (
    extract_json_from_response,
    generate_strategy_with_llm,
    display_strategy,
    confirm_strategy
)


class TestExtractJsonFromResponse:
    """Test JSON extraction from various response formats"""

    def test_extract_from_markdown_code_block(self):
        """Test extraction from markdown JSON code block"""
        text = '''Here's the strategy:

```json
{
  "agents": [{"name": "test-agent"}],
  "skills": [{"name": "test-skill"}],
  "rationale": "Test rationale"
}
```

That's the result.'''

        result = extract_json_from_response(text)
        parsed = json.loads(result)

        assert 'agents' in parsed
        assert 'skills' in parsed
        assert parsed['agents'][0]['name'] == 'test-agent'

    def test_extract_from_code_block_without_json_tag(self):
        """Test extraction from code block without 'json' language tag"""
        text = '''```
{
  "agents": [],
  "skills": [],
  "rationale": "Test"
}
```'''

        result = extract_json_from_response(text)
        parsed = json.loads(result)

        assert 'agents' in parsed
        assert 'skills' in parsed

    def test_extract_raw_json(self):
        """Test extraction of raw JSON without code blocks"""
        text = '''{"agents": [{"name": "test"}], "skills": [], "rationale": "Test"}'''

        result = extract_json_from_response(text)
        parsed = json.loads(result)

        assert 'agents' in parsed
        assert parsed['agents'][0]['name'] == 'test'

    def test_extract_json_with_text_before(self):
        """Test extraction when JSON appears after text"""
        text = '''Here's the strategy you requested:

{
  "agents": [{"name": "architect"}],
  "skills": [{"name": "test"}],
  "rationale": "Based on analysis"
}'''

        result = extract_json_from_response(text)
        parsed = json.loads(result)

        assert 'agents' in parsed
        assert parsed['agents'][0]['name'] == 'architect'

    def test_extract_json_with_nested_braces(self):
        """Test extraction handles nested braces in strings correctly"""
        text = '''{
  "agents": [{
    "name": "test",
    "purpose": "Handles {variables} in strings"
  }],
  "skills": [],
  "rationale": "Test with {braces}"
}'''

        result = extract_json_from_response(text)
        parsed = json.loads(result)

        assert parsed['agents'][0]['purpose'] == "Handles {variables} in strings"
        assert parsed['rationale'] == "Test with {braces}"


class TestGenerateStrategyWithLLM:
    """Test strategy generation with mocked Claude Code CLI"""

    @pytest.fixture
    def mock_analysis(self):
        """Sample codebase analysis for testing"""
        return {
            'tech_stacks': {
                'languages': ['python', 'javascript'],
                'frameworks': ['fastapi', 'react']
            },
            'testing': {
                'test_framework': 'pytest',
                'test_count': 42
            },
            'deployment': {
                'docker': True,
                'ci_cd': 'github_actions'
            },
            'structure': {
                'detected_layers': ['api', 'business', 'data'],
                'total_files': 150,
                'total_dirs': 25
            },
            'dependencies': {
                'critical': ['fastapi', 'pydantic', 'sqlalchemy']
            }
        }

    @pytest.fixture
    def mock_config(self):
        """Sample project config for testing"""
        return {
            'project': {
                'name': 'test-project'
            }
        }

    @pytest.mark.asyncio
    async def test_generate_strategy_success(self, mock_analysis, mock_config):
        """Test successful strategy generation"""
        mock_response = json.dumps({
            "agents": [
                {
                    "name": "test-architect",
                    "purpose": "Expert in codebase architecture",
                    "model": "sonnet",
                    "tools": ["Read", "Grep", "Glob"],
                    "color": "blue",
                    "rationale": "Needed for architecture questions"
                },
                {
                    "name": "test-guardian",
                    "purpose": "Enforces architectural standards",
                    "model": "sonnet",
                    "tools": ["Read", "Grep"],
                    "color": "orange",
                    "rationale": "Prevents antipatterns"
                }
            ],
            "skills": [
                {
                    "name": "test-architecture",
                    "purpose": "Show architectural overview",
                    "args": "",
                    "rationale": "Quick reference"
                }
            ],
            "rationale": "This project needs basic architecture support and standards enforcement."
        })

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response

            strategy = await generate_strategy_with_llm('test-project', mock_analysis, mock_config)

        # Verify strategy structure
        assert 'agents' in strategy
        assert 'skills' in strategy
        assert 'rationale' in strategy

        # Verify agents
        assert len(strategy['agents']) == 2
        assert strategy['agents'][0]['name'] == 'test-architect'
        assert strategy['agents'][1]['name'] == 'test-guardian'

        # Verify skills
        assert len(strategy['skills']) == 1
        assert strategy['skills'][0]['name'] == 'test-architecture'

        # Verify run_claude_code was called correctly
        mock_claude.assert_called_once()
        call_args = mock_claude.call_args

        # Check prompt (first argument)
        prompt = call_args[0][0]
        assert 'test-project' in prompt
        assert 'fastapi' in prompt
        assert 'pytest' in prompt

        # Check context (second argument)
        context = call_args[0][1]
        assert context['project'] == 'test-project'
        assert context['agent'] == 'strategy_generator'
        assert context['use_docker'] is False
        assert context['claude_model'] == 'claude-sonnet-4-5-20250929'

    @pytest.mark.asyncio
    async def test_generate_strategy_handles_dict_response(self, mock_analysis, mock_config):
        """Test handling of dict response from run_claude_code()"""
        mock_response_dict = {
            'result': json.dumps({
                "agents": [{"name": "test-agent"}],
                "skills": [{"name": "test-skill"}],
                "rationale": "Test"
            }),
            'session_id': 'session-123',
            'input_tokens': 1000,
            'output_tokens': 500
        }

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response_dict

            strategy = await generate_strategy_with_llm('test-project', mock_analysis, mock_config)

        assert 'agents' in strategy
        assert len(strategy['agents']) == 1
        assert strategy['agents'][0]['name'] == 'test-agent'

    @pytest.mark.asyncio
    async def test_generate_strategy_invalid_json(self, mock_analysis, mock_config):
        """Test error handling for invalid JSON response"""
        mock_response = "This is not valid JSON {invalid}"

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response

            with pytest.raises(ValueError) as exc_info:
                await generate_strategy_with_llm('test-project', mock_analysis, mock_config)

            assert 'Invalid JSON' in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_generate_strategy_empty_response(self, mock_analysis, mock_config):
        """Test error handling for empty response from Claude"""
        mock_response = ""

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response

            with pytest.raises(ValueError) as exc_info:
                await generate_strategy_with_llm('test-project', mock_analysis, mock_config)

            assert 'empty response' in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_strategy_whitespace_only_response(self, mock_analysis, mock_config):
        """Test error handling for whitespace-only response"""
        mock_response = "   \n\n   "

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response

            with pytest.raises(ValueError) as exc_info:
                await generate_strategy_with_llm('test-project', mock_analysis, mock_config)

            assert 'empty response' in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_strategy_malformed_analysis_dict(self, mock_config):
        """Test error handling for malformed analysis dict"""
        # Analysis missing required fields
        malformed_analysis = {
            'tech_stacks': {}  # Missing 'languages' and 'frameworks'
            # Missing 'testing', 'deployment', 'structure', 'dependencies'
        }

        # Should not raise an error due to safe .get() usage
        mock_response = json.dumps({
            "agents": [{"name": "test-agent"}],
            "skills": [{"name": "test-skill"}],
            "rationale": "Test"
        })

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response

            # Should succeed with safe defaults
            strategy = await generate_strategy_with_llm('test-project', malformed_analysis, mock_config)

            assert 'agents' in strategy
            # Verify the function handled missing fields gracefully

    @pytest.mark.asyncio
    async def test_generate_strategy_missing_fields(self, mock_analysis, mock_config):
        """Test error handling when strategy is missing required fields"""
        mock_response = json.dumps({
            "agents": [{"name": "test"}]
            # Missing 'skills' field
        })

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response

            with pytest.raises(ValueError) as exc_info:
                await generate_strategy_with_llm('test-project', mock_analysis, mock_config)

            assert 'missing required fields' in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_generate_strategy_saves_to_file(self, mock_analysis, mock_config, tmp_path, monkeypatch):
        """Test that strategy is saved to file"""
        # Set ORCHESTRATOR_ROOT to temp directory
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))

        mock_response = json.dumps({
            "agents": [{"name": "test-agent"}],
            "skills": [{"name": "test-skill"}],
            "rationale": "Test strategy"
        })

        with patch('scripts.generate_strategy.run_claude_code', new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = mock_response

            strategy = await generate_strategy_with_llm('test-project', mock_analysis, mock_config)

        # Verify file was created
        strategy_file = tmp_path / 'state' / 'projects' / 'test-project' / 'generation_strategy.json'
        assert strategy_file.exists()

        # Verify file contents
        with open(strategy_file, 'r') as f:
            saved_strategy = json.load(f)

        assert saved_strategy == strategy


class TestDisplayStrategy:
    """Test strategy display function"""

    def test_display_strategy(self, capsys):
        """Test strategy display output"""
        strategy = {
            "agents": [
                {
                    "name": "test-architect",
                    "purpose": "Expert in architecture",
                    "model": "sonnet",
                    "tools": ["Read", "Grep"]
                }
            ],
            "skills": [
                {
                    "name": "test-architecture",
                    "purpose": "Show overview",
                    "args": ""
                }
            ],
            "rationale": "Basic team for testing"
        }

        display_strategy('test-project', strategy)

        captured = capsys.readouterr()
        assert 'test-project' in captured.out
        assert 'test-architect' in captured.out
        assert 'test-architecture' in captured.out
        assert 'Basic team for testing' in captured.out


class TestConfirmStrategy:
    """Test strategy confirmation function"""

    def test_confirm_strategy_yes(self, monkeypatch):
        """Test user confirms strategy"""
        monkeypatch.setattr('builtins.input', lambda _: 'y')
        assert confirm_strategy() is True

    def test_confirm_strategy_no(self, monkeypatch):
        """Test user rejects strategy"""
        monkeypatch.setattr('builtins.input', lambda _: 'n')
        assert confirm_strategy() is False

    def test_confirm_strategy_default_yes(self, monkeypatch):
        """Test default (empty) input is treated as yes"""
        monkeypatch.setattr('builtins.input', lambda _: '')
        assert confirm_strategy() is True

    def test_confirm_strategy_retry_on_invalid(self, monkeypatch):
        """Test retry on invalid input"""
        inputs = iter(['invalid', 'maybe', 'yes'])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        assert confirm_strategy() is True
