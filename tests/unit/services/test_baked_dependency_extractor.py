"""
Unit tests for services/baked_dependency_extractor.py (issue #50).

Covers the docker create/cp/rm extraction mechanism directly:
- Extraction succeeds for a "new convention" image (BAKED_DEPS_PATH present).
- Extraction gracefully no-ops for an "old convention" image (BAKED_DEPS_PATH absent),
  without crashing.
- Extraction never raises on any Docker failure (create fails, cp fails for an
  unrelated reason, rm fails, or subprocess itself raises) -- callers (worktree
  creation) must never be blocked by this.

All Docker calls are mocked (subprocess.run) — no real docker commands run.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, Mock

from services.baked_dependency_extractor import extract_baked_dependencies, BAKED_DEPS_PATH


def _ok(stdout: str = "") -> Mock:
    result = Mock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _fail(stderr: str = "error") -> Mock:
    result = Mock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = stderr
    return result


class TestExtractionSucceedsForNewConventionImage:
    def test_create_cp_rm_sequence_and_true_on_success(self, tmp_path):
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _ok()]  # create, cp, rm

            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is True
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert len(calls) == 3

        assert calls[0][:2] == ['docker', 'create']
        assert calls[0][2] == '--name'
        container_name = calls[0][3]
        assert container_name.startswith("tmp-extract-my-project-")
        assert calls[0][4] == "my-project-agent:latest"

        assert calls[1] == ['docker', 'cp', f'{container_name}:{BAKED_DEPS_PATH}/.', str(destination)]
        assert calls[2] == ['docker', 'rm', '-f', container_name]

    def test_uses_unique_container_name_across_calls(self, tmp_path):
        """Concurrent extractions for the same project (e.g. two epics created in
        quick succession) must not collide on a fixed container name."""
        destination = tmp_path / "worktree"
        destination.mkdir()
        names = []
        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _ok()] * 2
            for _ in range(2):
                extract_baked_dependencies("my-project", "my-project-agent:latest", destination)
            for call in mock_run.call_args_list:
                if call.args[0][:2] == ['docker', 'create']:
                    names.append(call.args[0][3])
        assert len(set(names)) == 2


class TestExtractionSkipsGracefullyForOldConventionImage:
    def test_missing_baked_deps_path_returns_false_without_crashing(self, tmp_path):
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(),  # docker create succeeds
                _fail("Error: No such container:path: tmp-extract-my-project-abcd1234:/opt/deps"),
                _ok(),  # docker rm cleanup still runs
            ]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is False
        # cleanup (docker rm) still ran despite the missing path
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[-1][:2] == ['docker', 'rm']


class TestExtractionNeverBlocksWorktreeCreation:
    def test_docker_create_failure_returns_false_and_skips_rm(self, tmp_path):
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [_fail("Cannot connect to the Docker daemon")]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is False
        # Only the failed `docker create` call was made -- no cp, no rm (nothing to
        # remove; the container was never created).
        assert mock_run.call_count == 1
        assert mock_run.call_args_list[0].args[0][:2] == ['docker', 'create']

    def test_docker_cp_generic_failure_returns_false_and_still_cleans_up(self, tmp_path):
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _fail("unexpected EOF"), _ok()]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is False
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[-1].args[0][:2] == ['docker', 'rm']

    def test_docker_rm_failure_does_not_raise(self, tmp_path):
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _fail("no such container")]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is True  # extraction itself succeeded; only cleanup failed

    def test_subprocess_exception_is_caught_and_returns_false(self, tmp_path):
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd=['docker', 'create'], timeout=60),
                _ok(),  # rm cleanup attempt in finally
            ]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is False

    def test_create_raises_still_attempts_cleanup_since_container_may_exist(self, tmp_path):
        """A TimeoutExpired on 'docker create' itself doesn't prove the daemon
        never created the container server-side before the client gave up --
        cleanup must still be attempted (a `docker rm -f` on a genuinely
        nonexistent container is a harmless no-op; skipping it risked a
        permanent leak whenever the daemon actually had created it)."""
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd=['docker', 'create'], timeout=60),
                _ok(),  # rm cleanup attempt
            ]
            extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert mock_run.call_count == 2
        assert mock_run.call_args_list[-1].args[0][:2] == ['docker', 'rm']

    def test_create_confirmed_failed_skips_wasted_rm_call(self, tmp_path):
        """When 'docker create' explicitly reports failure (returncode != 0), we
        KNOW the container was never created -- attempting `docker rm -f` on it
        would be a harmless but wasted extra call, so it's skipped."""
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [_fail("Cannot connect to the Docker daemon")]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is False
        assert mock_run.call_count == 1

    def test_exception_after_create_still_attempts_rm_cleanup(self, tmp_path):
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(),  # docker create succeeds -> created=True
                subprocess.TimeoutExpired(cmd=['docker', 'cp'], timeout=60),  # cp raises
                _ok(),  # rm cleanup in finally
            ]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is False
        assert mock_run.call_args_list[-1].args[0][:2] == ['docker', 'rm']

    def test_rm_cleanup_exception_is_swallowed(self, tmp_path):
        """Even if the finally-block cleanup itself raises, extraction must not
        propagate that -- it already has its own try/except around the rm call."""
        destination = tmp_path / "worktree"
        destination.mkdir()

        with patch('services.baked_dependency_extractor.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(),
                _ok(),
                subprocess.TimeoutExpired(cmd=['docker', 'rm'], timeout=60),
            ]
            result = extract_baked_dependencies("my-project", "my-project-agent:latest", destination)

        assert result is True  # extraction succeeded; rm cleanup failure doesn't change that
