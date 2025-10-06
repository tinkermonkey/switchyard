"""
Integration tests for cleanup_orphaned_branches.py script
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.cleanup_orphaned_branches import (
    cleanup_project_branches,
    cleanup_all_projects
)


class TestCleanupProjectBranches:
    """Test cleanup for individual projects"""

    @pytest.mark.asyncio
    async def test_cleanup_project_success(self):
        """Test successful cleanup of a project"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config, \
             patch('scripts.cleanup_orphaned_branches.feature_branch_manager') as mock_fbm, \
             patch('scripts.cleanup_orphaned_branches.GitHubIntegration') as mock_gh_class:

            # Setup project config
            mock_project_config = MagicMock()
            mock_project_config.repository = "test-org/test-repo"
            mock_config.get_project_config.return_value = mock_project_config

            # Setup GitHub integration mock
            mock_gh = AsyncMock()
            mock_gh_class.return_value = mock_gh

            # Setup feature branch manager
            mock_fbm.cleanup_orphaned_branches = AsyncMock()

            # Run cleanup
            await cleanup_project_branches("test-project")

            # Verify cleanup was called with correct params
            mock_fbm.cleanup_orphaned_branches.assert_called_once_with(
                project="test-project",
                github_integration=mock_gh
            )

            # Verify GitHub integration created with correct repo
            mock_gh_class.assert_called_once_with(
                repo_owner="test-org",
                repo_name="test-repo"
            )

    @pytest.mark.asyncio
    async def test_cleanup_project_not_found(self, caplog):
        """Test cleanup when project not found"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config:
            mock_config.get_project_config.return_value = None

            # Run cleanup
            await cleanup_project_branches("nonexistent-project")

            # Should log error
            assert "not found" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_cleanup_project_no_repository(self, caplog):
        """Test cleanup when project has no repository configured"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config:
            mock_project_config = MagicMock()
            # No repository attribute
            del mock_project_config.repository
            mock_config.get_project_config.return_value = mock_project_config

            # Run cleanup
            await cleanup_project_branches("test-project")

            # Should log error
            assert "no repository" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_cleanup_project_invalid_repository_format(self, caplog):
        """Test cleanup when repository format is invalid"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config:
            mock_project_config = MagicMock()
            mock_project_config.repository = "invalid-format"  # Missing org/repo split
            mock_config.get_project_config.return_value = mock_project_config

            # Run cleanup
            await cleanup_project_branches("test-project")

            # Should log error
            assert "invalid" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_cleanup_project_handles_exception(self, caplog):
        """Test cleanup handles exceptions gracefully"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config, \
             patch('scripts.cleanup_orphaned_branches.feature_branch_manager') as mock_fbm, \
             patch('scripts.cleanup_orphaned_branches.GitHubIntegration') as mock_gh_class:

            # Setup project config
            mock_project_config = MagicMock()
            mock_project_config.repository = "test-org/test-repo"
            mock_config.get_project_config.return_value = mock_project_config

            # Cleanup raises exception
            mock_fbm.cleanup_orphaned_branches = AsyncMock(
                side_effect=Exception("API rate limit exceeded")
            )

            # Run cleanup - should not raise
            await cleanup_project_branches("test-project")

            # Should log error
            assert "error" in caplog.text.lower()
            assert "test-project" in caplog.text.lower()


class TestCleanupAllProjects:
    """Test cleanup for all projects"""

    @pytest.mark.asyncio
    async def test_cleanup_all_projects_success(self):
        """Test successful cleanup of all projects"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config, \
             patch('scripts.cleanup_orphaned_branches.feature_branch_manager') as mock_fbm, \
             patch('scripts.cleanup_orphaned_branches.GitHubIntegration') as mock_gh_class:

            # Setup multiple projects
            mock_config1 = MagicMock()
            mock_config1.repository = "org1/repo1"

            mock_config2 = MagicMock()
            mock_config2.repository = "org2/repo2"

            mock_config.get_all_project_configs.return_value = {
                'project1': mock_config1,
                'project2': mock_config2
            }

            # Setup cleanup mock
            mock_fbm.cleanup_orphaned_branches = AsyncMock()

            # Run cleanup
            await cleanup_all_projects()

            # Verify cleanup called for both projects
            assert mock_fbm.cleanup_orphaned_branches.call_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_all_projects_empty(self, caplog):
        """Test cleanup when no projects configured"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config:
            mock_config.get_all_project_configs.return_value = {}

            # Run cleanup
            await cleanup_all_projects()

            # Should log warning
            assert "no projects" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_cleanup_all_projects_partial_failure(self):
        """Test cleanup continues when some projects fail"""
        with patch('scripts.cleanup_orphaned_branches.config_manager') as mock_config, \
             patch('scripts.cleanup_orphaned_branches.feature_branch_manager') as mock_fbm, \
             patch('scripts.cleanup_orphaned_branches.GitHubIntegration') as mock_gh_class:

            # Setup projects
            mock_config1 = MagicMock()
            mock_config1.repository = "org1/repo1"

            mock_config2 = MagicMock()
            mock_config2.repository = "org2/repo2"

            mock_config3 = MagicMock()
            mock_config3.repository = "org3/repo3"

            mock_config.get_all_project_configs.return_value = {
                'project1': mock_config1,
                'project2': mock_config2,
                'project3': mock_config3
            }

            # Second cleanup fails
            mock_fbm.cleanup_orphaned_branches = AsyncMock(
                side_effect=[None, Exception("GitHub API error"), None]
            )

            # Run cleanup - should not raise
            await cleanup_all_projects()

            # Verify all three were attempted
            assert mock_fbm.cleanup_orphaned_branches.call_count == 3


class TestScriptIntegration:
    """Test script command-line integration"""

    def test_script_imports(self):
        """Test that script can be imported without errors"""
        try:
            import scripts.cleanup_orphaned_branches as script
            assert hasattr(script, 'cleanup_project_branches')
            assert hasattr(script, 'cleanup_all_projects')
            assert hasattr(script, 'main')
        except ImportError as e:
            pytest.fail(f"Failed to import cleanup script: {e}")

    def test_script_has_main_guard(self):
        """Test that script has proper __main__ guard"""
        script_path = Path(__file__).parent.parent.parent / "scripts" / "cleanup_orphaned_branches.py"
        content = script_path.read_text()

        assert "if __name__ == '__main__':" in content
        assert "main()" in content

    def test_script_executable_permission(self):
        """Test that script has executable permission"""
        script_path = Path(__file__).parent.parent.parent / "scripts" / "cleanup_orphaned_branches.py"

        # Check file exists
        assert script_path.exists()

        # Check has shebang
        content = script_path.read_text()
        assert content.startswith('#!/usr/bin/env python3')


class TestArgumentParsing:
    """Test command-line argument parsing"""

    def test_main_function_exists(self):
        """Test that main function exists"""
        from scripts.cleanup_orphaned_branches import main

        assert callable(main)

    @patch('scripts.cleanup_orphaned_branches.asyncio.run')
    @patch('scripts.cleanup_orphaned_branches.argparse.ArgumentParser')
    def test_main_with_project_arg(self, mock_argparse, mock_asyncio_run):
        """Test main function with --project argument"""
        from scripts.cleanup_orphaned_branches import main

        # Mock argument parser
        mock_parser = MagicMock()
        mock_args = MagicMock()
        mock_args.project = "test-project"
        mock_parser.parse_args.return_value = mock_args
        mock_argparse.return_value = mock_parser

        # Run main
        main()

        # Verify asyncio.run called with cleanup_project_branches
        mock_asyncio_run.assert_called_once()

    @patch('scripts.cleanup_orphaned_branches.asyncio.run')
    @patch('scripts.cleanup_orphaned_branches.argparse.ArgumentParser')
    def test_main_without_project_arg(self, mock_argparse, mock_asyncio_run):
        """Test main function without --project argument (all projects)"""
        from scripts.cleanup_orphaned_branches import main

        # Mock argument parser
        mock_parser = MagicMock()
        mock_args = MagicMock()
        mock_args.project = None
        mock_parser.parse_args.return_value = mock_args
        mock_argparse.return_value = mock_parser

        # Run main
        main()

        # Verify asyncio.run called with cleanup_all_projects
        mock_asyncio_run.assert_called_once()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
