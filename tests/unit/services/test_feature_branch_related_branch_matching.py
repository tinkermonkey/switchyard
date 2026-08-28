"""
Regression test for FeatureBranchManager.find_related_branches()'s numeric-prefix
substring bug, found in production against tinkermonkey/phone-home.

Issue #2 (an epic with sub-issues #257-260) had its work silently attached to
branch `feature/issue-216-token-efficiency-program-trac` — a fully-merged,
completely unrelated branch for issue #216. The resulting PR (#273) was
correctly titled/bodied for #2's work, but built on the wrong branch, so
pipeline/pr_review_stage.py's `feature/issue-2-` prefix lookup could never
find it — the PR review stage failed identically ("No PR found for parent
issue #2") on every retry from 2026-08-26 onward.

Root cause: both the "exact issue number" and "parent issue branch" match
checks used `f"issue-{n}" in branch`, a plain substring test. Since "216"
starts with the digit "2", the substring "issue-2" is contained in
"feature/issue-216-...", scoring a false-positive match at confidence 0.95-1.0
— comfortably above prepare_feature_branch()'s 0.8 auto-reuse-without-
human-review threshold, so the wrong branch was reused silently, with no
escalation.
"""

import pytest
from unittest.mock import AsyncMock, patch

from services.feature_branch_manager import FeatureBranchManager


class TestFindRelatedBranchesNumericPrefixBug:
    @pytest.fixture
    def manager(self):
        return FeatureBranchManager()

    @pytest.mark.asyncio
    async def test_does_not_match_a_branch_whose_number_starts_with_the_same_digits(self, manager):
        """The exact regression case: issue #2 must not match
        feature/issue-216-... (or issue-20-, issue-200-, etc.) via check 1."""
        with patch.object(
            manager, 'get_all_feature_branches_for_project',
            new=AsyncMock(return_value=[
                'feature/issue-216-token-efficiency-program-trac',
                'feature/issue-20-something-else',
                'feature/issue-200-yet-another-thing',
            ]),
        ):
            matches = await manager.find_related_branches(
                project='phone-home', project_dir='/workspace/phone-home',
                issue_number=2, issue_title='deterministic health-check script',
            )

        assert matches == []

    @pytest.mark.asyncio
    async def test_still_matches_the_correctly_named_branch(self, manager):
        """Control case: the fix must not break the actual intended match —
        a genuine feature/issue-2-... branch must still be found."""
        with patch.object(
            manager, 'get_all_feature_branches_for_project',
            new=AsyncMock(return_value=[
                'feature/issue-2-deterministic-health-check',
                'feature/issue-216-token-efficiency-program-trac',
            ]),
        ):
            matches = await manager.find_related_branches(
                project='phone-home', project_dir='/workspace/phone-home',
                issue_number=2, issue_title='deterministic health-check script',
            )

        assert len(matches) == 1
        assert matches[0]['branch_name'] == 'feature/issue-2-deterministic-health-check'
        assert matches[0]['match_type'] == 'exact_issue'
        assert matches[0]['confidence'] == 1.0

    @pytest.mark.asyncio
    async def test_parent_issue_match_does_not_hit_the_same_numeric_prefix_bug(self, manager):
        """Same bug, same fix, for check 2 (parent_issue) — this is the
        sub-issue path: a sub-issue of epic #2 must not silently reuse #216's
        branch either."""
        with patch.object(
            manager, 'get_all_feature_branches_for_project',
            new=AsyncMock(return_value=[
                'feature/issue-216-token-efficiency-program-trac',
            ]),
        ):
            matches = await manager.find_related_branches(
                project='phone-home', project_dir='/workspace/phone-home',
                issue_number=257, issue_title='Phase 1: new network probes',
                parent_issue=2,
            )

        assert matches == []

    @pytest.mark.asyncio
    async def test_parent_issue_still_matches_its_correct_branch(self, manager):
        """Control case for check 2: a genuine parent branch must still match."""
        with patch.object(
            manager, 'get_all_feature_branches_for_project',
            new=AsyncMock(return_value=[
                'feature/issue-2-deterministic-health-check',
            ]),
        ):
            matches = await manager.find_related_branches(
                project='phone-home', project_dir='/workspace/phone-home',
                issue_number=257, issue_title='Phase 1: new network probes',
                parent_issue=2,
            )

        assert len(matches) == 1
        assert matches[0]['branch_name'] == 'feature/issue-2-deterministic-health-check'
        assert matches[0]['match_type'] == 'parent_branch'
        assert matches[0]['confidence'] == 0.95
