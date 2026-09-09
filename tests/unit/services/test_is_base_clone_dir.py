"""
Tests for ProjectWorkspaceManager.is_base_clone_dir() (issue #54, Phase 2 of
the concurrency redesign, #88/#34).

No existing test coverage for this method at all before this file -- added
during #54's review (round 3) alongside a real gap it found: the method's
own docstring promises to fail closed (return True) if path resolution
raises OR the directory doesn't exist, but Path.resolve() succeeds without
error even for a nonexistent path, so only the exception branch was actually
covered by that promise before this fix.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from services.project_workspace import ProjectWorkspaceManager


class TestIsBaseCloneDir(unittest.TestCase):
    def setUp(self):
        self.workspace_root = Path(tempfile.mkdtemp())
        self.manager = ProjectWorkspaceManager(workspace_root=self.workspace_root)
        self.base_clone_dir = self.workspace_root / "proj"
        self.base_clone_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.workspace_root, ignore_errors=True)

    def test_true_for_the_real_base_clone_dir(self):
        self.assertTrue(self.manager.is_base_clone_dir("proj", self.base_clone_dir))

    def test_true_for_an_equivalent_but_differently_written_path(self):
        # relative-looking / trailing-slash / non-normalized form of the same
        # real directory must still resolve to the same match.
        equivalent = self.workspace_root / "proj" / "." / ""
        self.assertTrue(self.manager.is_base_clone_dir("proj", equivalent))

    def test_false_for_a_different_existing_directory(self):
        other_dir = self.workspace_root / "proj" / "some-epic-worktree"
        other_dir.mkdir()
        self.assertFalse(self.manager.is_base_clone_dir("proj", other_dir))

    def test_fails_closed_true_when_directory_does_not_exist(self):
        """
        The real bug this covers: a caller's unset/default placeholder (e.g.
        claude_integration.py's `Path(context.get('work_dir', '.'))` when
        'work_dir' is absent) resolves without error via Path.resolve() even
        though nothing exists there -- must still fail closed (True), not
        silently compare-and-return-False.
        """
        nonexistent = self.workspace_root / "proj" / "does-not-exist-at-all"
        self.assertFalse(nonexistent.exists())

        self.assertTrue(self.manager.is_base_clone_dir("proj", nonexistent))

    def test_fails_closed_true_on_resolve_exception(self):
        # A path containing a NUL byte makes Path.resolve() raise on POSIX.
        with self.assertRaises(ValueError):
            Path("bad\x00path").resolve()  # confirm the premise this test relies on

        self.assertTrue(self.manager.is_base_clone_dir("proj", "bad\x00path"))


if __name__ == '__main__':
    unittest.main()
