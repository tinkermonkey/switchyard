"""
Unit tests for review cycle context extraction

These tests verify that _get_fresh_discussion_context() correctly
extracts the maker's latest output for the reviewer, preventing
context bloat and incorrect context bugs.

Regression test for bug where iteration 3 reviewer received its own
previous output instead of the maker's revision.
"""

import pytest
from datetime import datetime
from services.review_cycle import ReviewCycleExecutor, ReviewCycleState


@pytest.fixture
def review_cycle_executor():
    """Create a ReviewCycleExecutor instance for testing"""
    return ReviewCycleExecutor()


@pytest.fixture
def mock_cycle_state():
    """Create a basic ReviewCycleState for testing"""
    return ReviewCycleState(
        issue_number=96,
        repository='context-studio',
        maker_agent='business_analyst',
        reviewer_agent='requirements_reviewer',
        max_iterations=3,
        project_name='context-studio',
        board_name='idea-development',
        workspace_type='discussions',
        discussion_id='D_kwDOPH6wk84AiPtN'
    )


@pytest.fixture
def discussion_with_3_iterations():
    """
    Mock discussion data representing 3 complete review iterations:
    - Iteration 1: BA initial → RR review → BA revision 1
    - Iteration 2: RR review 2 → BA revision 2
    - Iteration 3: RR review 3 (should see BA revision 2)
    """
    return {
        'node': {
            'comments': {
                'nodes': [
                    # Initial BA output
                    {
                        'id': 'comment_1',
                        'body': '## Business Requirements\n\n[12KB of content]\n\n_Processed by the business_analyst agent_',
                        'author': {'login': 'orchestrator-bot'},
                        'createdAt': '2025-10-03T13:00:00Z',
                        'replies': {'nodes': []}
                    },
                    # Reviewer iteration 1
                    {
                        'id': 'comment_2',
                        'body': '## Issues Found\n\n- Missing details on X\n\n_Processed by the requirements_reviewer agent_',
                        'author': {'login': 'orchestrator-bot'},
                        'createdAt': '2025-10-03T13:05:00Z',
                        'replies': {'nodes': []}
                    },
                    # BA revision 1
                    {
                        'id': 'comment_3',
                        'body': '## Revision Notes\n- Added X details\n\n[13KB of content]\n\n_Processed by the business_analyst agent_',
                        'author': {'login': 'orchestrator-bot'},
                        'createdAt': '2025-10-03T13:10:00Z',
                        'replies': {'nodes': []}
                    },
                    # Reviewer iteration 2
                    {
                        'id': 'comment_4',
                        'body': '## Issues Found\n\n- Still missing Y\n\n_Processed by the requirements_reviewer agent_',
                        'author': {'login': 'orchestrator-bot'},
                        'createdAt': '2025-10-03T13:15:00Z',
                        'replies': {'nodes': []}
                    },
                    # BA revision 2 (LATEST MAKER OUTPUT)
                    {
                        'id': 'comment_5',
                        'body': '## Revision Notes\n- Added Y details\n\n[14KB of content]\n\n_Processed by the business_analyst agent_',
                        'author': {'login': 'orchestrator-bot'},
                        'createdAt': '2025-10-03T13:20:00Z',
                        'replies': {'nodes': []}
                    },
                    # Reviewer iteration 3
                    {
                        'id': 'comment_6',
                        'body': '## Issues Found\n\n- Same issues as before\n\n_Processed by the requirements_reviewer agent_',
                        'author': {'login': 'orchestrator-bot'},
                        'createdAt': '2025-10-03T13:25:00Z',
                        'replies': {'nodes': []}
                    }
                ]
            }
        }
    }


class TestReviewCycleContextExtraction:
    """Test context extraction logic for review cycles"""

    @pytest.mark.asyncio
    async def test_reviewer_always_gets_last_maker_output(
        self,
        review_cycle_executor,
        mock_cycle_state,
        discussion_with_3_iterations,
        monkeypatch
    ):
        """
        REGRESSION TEST: Reviewer should ALWAYS receive the maker's
        latest output, regardless of iteration number.

        Bug: Iteration 3 reviewer was receiving its own previous output
        (comment_4, "Issues Found") instead of maker's revision (comment_5).

        Fix: _get_fresh_discussion_context always looks for maker_agent,
        not alternating based on iteration number.
        """
        # Mock GitHub API to return our test discussion
        def mock_graphql_request(query, variables):
            return discussion_with_3_iterations

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql_request)

        # When: Reviewer is about to execute iteration 4
        mock_cycle_state.current_iteration = 3
        context = await review_cycle_executor._get_fresh_discussion_context(
            mock_cycle_state,
            org='tinkermonkey',
            iteration=4  # Next iteration where reviewer will run
        )

        # Then: Should contain maker's latest output (comment_5)
        assert '_Processed by the business_analyst agent_' in context
        assert '## Revision Notes' in context
        assert 'Added Y details' in context

        # And: Should NOT contain reviewer's output
        assert '## Issues Found' not in context or context.count('## Issues Found') == 0

        # And: Should contain exactly ONE maker signature (not all previous iterations)
        assert context.count('_Processed by the business_analyst agent_') == 1

        # And: Should be reasonable size (one comment, not entire discussion)
        assert len(context) < 20000, f"Context too large: {len(context)} chars"


    @pytest.mark.asyncio
    async def test_context_extraction_for_iteration_1(
        self,
        review_cycle_executor,
        mock_cycle_state,
        monkeypatch
    ):
        """
        Test: First review iteration should get initial maker output
        """
        # Given: Discussion with only initial BA output
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': '## Initial Analysis\n\n[Content]\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': '2025-10-03T13:00:00Z',
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }

        def mock_graphql_request(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql_request)

        # When: Extract context for iteration 1
        mock_cycle_state.current_iteration = 0
        context = await review_cycle_executor._get_fresh_discussion_context(
            mock_cycle_state,
            org='tinkermonkey',
            iteration=1
        )

        # Then: Should get initial BA output
        assert '## Initial Analysis' in context
        assert '_Processed by the business_analyst agent_' in context


    @pytest.mark.asyncio
    async def test_context_extraction_excludes_human_comments(
        self,
        review_cycle_executor,
        mock_cycle_state,
        monkeypatch
    ):
        """
        Test: Context should only include bot comments, not human feedback
        (Human feedback is handled separately)
        """
        # Given: Discussion with BA output and human comments
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': '2025-10-03T13:00:00Z',
                            'replies': {'nodes': [
                                {
                                    'id': 'reply_1',
                                    'body': 'Human question here',
                                    'author': {'login': 'tinkermonkey'},
                                    'createdAt': '2025-10-03T13:05:00Z'
                                }
                            ]}
                        },
                        {
                            'id': 'comment_2',
                            'body': 'BA revision\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': '2025-10-03T13:10:00Z',
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }

        def mock_graphql_request(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql_request)

        # When: Extract context
        mock_cycle_state.current_iteration = 1
        context = await review_cycle_executor._get_fresh_discussion_context(
            mock_cycle_state,
            org='tinkermonkey',
            iteration=2
        )

        # Then: Should get latest BA output only
        assert 'BA revision' in context
        assert 'Human question' not in context


    @pytest.mark.asyncio
    async def test_context_extraction_finds_last_not_first_maker_output(
        self,
        review_cycle_executor,
        mock_cycle_state,
        monkeypatch
    ):
        """
        Test: With multiple maker outputs, should return LAST one,
        not first or any other iteration.
        """
        # Given: Discussion with 5 BA outputs
        ba_outputs = [
            f'BA output {i}\n\n_Processed by the business_analyst agent_'
            for i in range(1, 6)
        ]

        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': f'comment_{i}',
                            'body': output,
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': f'2025-10-03T13:{i:02d}:00Z',
                            'replies': {'nodes': []}
                        }
                        for i, output in enumerate(ba_outputs, 1)
                    ]
                }
            }
        }

        def mock_graphql_request(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql_request)

        # When: Extract context
        mock_cycle_state.current_iteration = 4
        context = await review_cycle_executor._get_fresh_discussion_context(
            mock_cycle_state,
            org='tinkermonkey',
            iteration=5
        )

        # Then: Should get LAST BA output (output 5)
        assert 'BA output 5' in context
        assert 'BA output 1' not in context
        assert 'BA output 2' not in context
        assert 'BA output 3' not in context
        assert 'BA output 4' not in context


    def test_context_extraction_signature_detection(self):
        """
        Test: Agent signature detection correctly identifies maker vs reviewer
        """
        ba_signature = '_Processed by the business_analyst agent_'
        rr_signature = '_Processed by the requirements_reviewer agent_'

        ba_comment = f"Some content\n\n{ba_signature}"
        rr_comment = f"Review content\n\n{rr_signature}"

        assert ba_signature in ba_comment
        assert ba_signature not in rr_comment
        assert rr_signature in rr_comment
        assert rr_signature not in ba_comment


class TestContextSizeLimits:
    """Test that extracted context stays within reasonable bounds"""

    @pytest.mark.asyncio
    async def test_context_not_bloated_with_entire_discussion(
        self,
        review_cycle_executor,
        mock_cycle_state,
        discussion_with_3_iterations,
        monkeypatch
    ):
        """
        Test: Context should be ONE comment, not accumulated discussion
        """
        def mock_graphql_request(query, variables):
            return discussion_with_3_iterations

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql_request)

        # When: Extract context at any iteration
        for iteration in range(1, 4):
            mock_cycle_state.current_iteration = iteration - 1
            context = await review_cycle_executor._get_fresh_discussion_context(
                mock_cycle_state,
                org='tinkermonkey',
                iteration=iteration
            )

            # Then: Should be reasonable size
            assert len(context) < 50000, \
                f"Iteration {iteration}: Context too large ({len(context)} chars)"

            # And: Should contain only ONE agent signature
            signature_count = context.count('_Processed by the business_analyst agent_')
            assert signature_count == 1, \
                f"Iteration {iteration}: Expected 1 signature, found {signature_count}"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
