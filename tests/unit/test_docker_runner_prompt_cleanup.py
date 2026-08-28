"""
Unit tests for docker_runner.py prompt file cleanup.

Covers issue #39: prompt-file cleanup must be scoped to the current run's own
prompt_path, not a glob-delete of every .claude_prompt_*.txt in project_dir
(which would delete a concurrently-running task's still-in-flight prompt file
sharing the same project_dir).
"""

from pathlib import Path

from claude.docker_runner import DockerAgentRunner


class TestCleanupPromptFile:
    """Test DockerAgentRunner._cleanup_prompt_file scoping."""

    def test_removes_only_its_own_prompt_file(self, tmp_path):
        """Cleanup deletes the given run's prompt file and leaves a sibling
        run's prompt file (simulating a concurrent task sharing project_dir)
        untouched."""
        runner = DockerAgentRunner()

        this_run_prompt = tmp_path / ".claude_prompt_task-a.txt"
        other_run_prompt = tmp_path / ".claude_prompt_task-b.txt"
        this_run_prompt.write_text("prompt for task a")
        other_run_prompt.write_text("prompt for task b, still in flight")

        runner._cleanup_prompt_file(this_run_prompt)

        assert not this_run_prompt.exists(), "this run's own prompt file should be removed"
        assert other_run_prompt.exists(), "a concurrent run's prompt file must survive cleanup"
        assert other_run_prompt.read_text() == "prompt for task b, still in flight"

    def test_missing_file_does_not_raise(self, tmp_path):
        """Cleanup is a no-op (not an error) when the prompt file is already gone."""
        runner = DockerAgentRunner()

        missing_prompt = tmp_path / ".claude_prompt_already-gone.txt"
        assert not missing_prompt.exists()

        # Should not raise
        runner._cleanup_prompt_file(missing_prompt)

    def test_context_label_does_not_change_target_file(self, tmp_path):
        """The context_label (used only for logging: exception/timeout/etc.) has
        no bearing on which file gets deleted, and other files are still spared."""
        runner = DockerAgentRunner()

        this_run_prompt = tmp_path / ".claude_prompt_task-c.txt"
        other_run_prompt = tmp_path / ".claude_prompt_task-d.txt"
        this_run_prompt.write_text("prompt c")
        other_run_prompt.write_text("prompt d")

        runner._cleanup_prompt_file(this_run_prompt, context_label="timeout")

        assert not this_run_prompt.exists()
        assert other_run_prompt.exists()

    def test_cleanup_never_globs_project_dir(self, tmp_path):
        """Regression guard: cleanup must not sweep every .claude_prompt_*.txt in
        the directory. Three concurrent runs' prompt files sit side by side;
        cleaning up just one must leave the other two exactly as they were."""
        runner = DockerAgentRunner()

        prompts = {
            name: tmp_path / f".claude_prompt_{name}.txt"
            for name in ("run-1", "run-2", "run-3")
        }
        for name, path in prompts.items():
            path.write_text(f"prompt for {name}")

        runner._cleanup_prompt_file(prompts["run-2"])

        assert not prompts["run-2"].exists()
        assert prompts["run-1"].exists()
        assert prompts["run-1"].read_text() == "prompt for run-1"
        assert prompts["run-3"].exists()
        assert prompts["run-3"].read_text() == "prompt for run-3"
