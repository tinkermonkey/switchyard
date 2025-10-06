"""
Test builders for creating test data structures

Provides fluent interfaces for building complex test objects
without verbose setup code in every test.
"""

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from services.review_cycle import ReviewCycleState


class ReviewCycleStateBuilder:
    """
    Fluent builder for ReviewCycleState test objects

    Example:
        cycle = (ReviewCycleStateBuilder()
            .for_issue(96)
            .with_agents('business_analyst', 'requirements_reviewer')
            .at_iteration(2)
            .with_maker_output("BA revision 1")
            .with_maker_output("BA revision 2")
            .with_review_output("RR feedback 1")
            .escalated()
            .build())
    """

    def __init__(self):
        self._issue_number = 1
        self._repository = 'test-repo'
        self._maker_agent = 'business_analyst'
        self._reviewer_agent = 'requirements_reviewer'
        self._max_iterations = 3
        self._project_name = 'test-project'
        self._board_name = 'test-board'
        self._workspace_type = 'discussions'
        self._discussion_id = 'D_test123'
        self._current_iteration = 0
        self._maker_outputs = []
        self._review_outputs = []
        self._status = 'initialized'
        self._escalation_time = None

    def for_issue(self, issue_number: int):
        """Set issue number"""
        self._issue_number = issue_number
        return self

    def in_repository(self, repository: str):
        """Set repository"""
        self._repository = repository
        return self

    def with_agents(self, maker: str, reviewer: str):
        """Set maker and reviewer agents"""
        self._maker_agent = maker
        self._reviewer_agent = reviewer
        return self

    def for_project(self, project_name: str, board_name: str = 'main-board'):
        """Set project and board names"""
        self._project_name = project_name
        self._board_name = board_name
        return self

    def in_discussion(self, discussion_id: str):
        """Set discussion ID"""
        self._discussion_id = discussion_id
        self._workspace_type = 'discussions'
        return self

    def in_issues(self):
        """Use issues workspace"""
        self._workspace_type = 'issues'
        self._discussion_id = None
        return self

    def with_max_iterations(self, max_iterations: int):
        """Set max iterations"""
        self._max_iterations = max_iterations
        return self

    def at_iteration(self, iteration: int):
        """Set current iteration"""
        self._current_iteration = iteration
        return self

    def with_maker_output(self, output: str, iteration: Optional[int] = None):
        """Add a maker output"""
        if iteration is None:
            iteration = len(self._maker_outputs)

        self._maker_outputs.append({
            'iteration': iteration,
            'output': output,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
        return self

    def with_review_output(self, output: str, iteration: Optional[int] = None):
        """Add a review output"""
        if iteration is None:
            iteration = len(self._review_outputs)

        self._review_outputs.append({
            'iteration': iteration,
            'output': output,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
        return self

    def with_status(self, status: str):
        """Set status"""
        self._status = status
        return self

    def initialized(self):
        """Set status to initialized"""
        self._status = 'initialized'
        return self

    def maker_working(self):
        """Set status to maker_working"""
        self._status = 'maker_working'
        return self

    def reviewer_working(self):
        """Set status to reviewer_working"""
        self._status = 'reviewer_working'
        return self

    def escalated(self, escalation_time: Optional[str] = None):
        """Set status to awaiting_human_feedback"""
        self._status = 'awaiting_human_feedback'
        self._escalation_time = escalation_time or datetime.now(timezone.utc).isoformat()
        return self

    def completed(self):
        """Set status to completed"""
        self._status = 'completed'
        return self

    def build(self) -> ReviewCycleState:
        """Build the ReviewCycleState object"""
        state = ReviewCycleState(
            issue_number=self._issue_number,
            repository=self._repository,
            maker_agent=self._maker_agent,
            reviewer_agent=self._reviewer_agent,
            max_iterations=self._max_iterations,
            project_name=self._project_name,
            board_name=self._board_name,
            workspace_type=self._workspace_type,
            discussion_id=self._discussion_id
        )

        state.current_iteration = self._current_iteration
        state.maker_outputs = self._maker_outputs
        state.review_outputs = self._review_outputs
        state.status = self._status
        state.escalation_time = self._escalation_time

        return state


class DiscussionBuilder:
    """
    Fluent builder for GitHub discussion GraphQL responses

    Example:
        discussion = (DiscussionBuilder()
            .with_id('D_abc123')
            .with_comment('orchestrator-bot', 'BA output', is_ba=True)
            .with_comment('orchestrator-bot', 'RR feedback', is_reviewer=True)
            .with_reply('tinkermonkey', 'Human question', to_comment=0)
            .build())
    """

    def __init__(self):
        self._discussion_id = 'D_test123'
        self._number = 1
        self._title = 'Test Discussion'
        self._comments = []
        self._comment_counter = 1

    def with_id(self, discussion_id: str):
        """Set discussion ID"""
        self._discussion_id = discussion_id
        return self

    def with_number(self, number: int):
        """Set discussion number"""
        self._number = number
        return self

    def with_title(self, title: str):
        """Set discussion title"""
        self._title = title
        return self

    def with_comment(
        self,
        author: str,
        body: str,
        is_ba: bool = False,
        is_reviewer: bool = False,
        created_at: Optional[str] = None
    ):
        """
        Add a top-level comment

        Args:
            author: Comment author login
            body: Comment body text
            is_ba: Automatically append BA agent signature
            is_reviewer: Automatically append reviewer agent signature
            created_at: ISO timestamp (defaults to now)
        """
        if is_ba:
            body = f"{body}\n\n_Processed by the business_analyst agent_"
        elif is_reviewer:
            body = f"{body}\n\n_Processed by the requirements_reviewer agent_"

        comment_id = f"comment_{self._comment_counter}"
        self._comment_counter += 1

        self._comments.append({
            'id': comment_id,
            'body': body,
            'author': {'login': author},
            'createdAt': created_at or datetime.now(timezone.utc).isoformat(),
            'replies': {'nodes': []}
        })
        return self

    def with_reply(
        self,
        author: str,
        body: str,
        to_comment: int,
        created_at: Optional[str] = None
    ):
        """
        Add a reply to a specific comment

        Args:
            author: Reply author login
            body: Reply body text
            to_comment: Index of parent comment (0-based)
            created_at: ISO timestamp (defaults to now)
        """
        if to_comment >= len(self._comments):
            raise ValueError(f"Comment index {to_comment} out of range (have {len(self._comments)} comments)")

        reply_id = f"reply_{self._comment_counter}"
        self._comment_counter += 1

        self._comments[to_comment]['replies']['nodes'].append({
            'id': reply_id,
            'body': body,
            'author': {'login': author},
            'createdAt': created_at or datetime.now(timezone.utc).isoformat()
        })
        return self

    def build(self) -> Dict[str, Any]:
        """Build the discussion GraphQL response"""
        return {
            'id': self._discussion_id,
            'number': self._number,
            'title': self._title,
            'comments': {
                'nodes': self._comments
            }
        }


class TaskContextBuilder:
    """
    Fluent builder for agent task context dictionaries

    Example:
        context = (TaskContextBuilder()
            .for_project('context-studio')
            .for_issue(96)
            .in_discussion('D_abc123')
            .with_trigger('review_cycle')
            .with_column('Review')
            .with_previous_output('BA output...')
            .build())
    """

    def __init__(self):
        self._context = {
            'timestamp': datetime.now().isoformat()
        }

    def for_project(self, project_name: str, board_name: str = 'main-board'):
        """Set project and board"""
        self._context['project'] = project_name
        self._context['board'] = board_name
        return self

    def for_repository(self, repository: str):
        """Set repository"""
        self._context['repository'] = repository
        return self

    def for_issue(self, issue_number: int, title: str = 'Test Issue', body: str = 'Test body'):
        """Set issue information"""
        self._context['issue_number'] = issue_number
        self._context['issue'] = {
            'number': issue_number,
            'title': title,
            'body': body
        }
        return self

    def in_discussion(self, discussion_id: str):
        """Set discussion workspace"""
        self._context['workspace_type'] = 'discussions'
        self._context['discussion_id'] = discussion_id
        return self

    def in_issues(self):
        """Set issues workspace"""
        self._context['workspace_type'] = 'issues'
        return self

    def with_trigger(self, trigger: str):
        """Set trigger type"""
        self._context['trigger'] = trigger
        return self

    def with_column(self, column_name: str):
        """Set column name"""
        self._context['column'] = column_name
        return self

    def with_agent(self, agent_name: str):
        """Set agent name"""
        self._context['agent'] = agent_name
        return self

    def with_previous_output(self, output: str):
        """Set previous stage output"""
        self._context['previous_stage_output'] = output
        return self

    def with_feedback(self, feedback: str):
        """Set human feedback"""
        self._context['feedback'] = {'formatted_text': feedback}
        return self

    def with_review_cycle(self, iteration: int, max_iterations: int, maker: str, reviewer: str):
        """Set review cycle information"""
        self._context['review_cycle'] = {
            'iteration': iteration,
            'max_iterations': max_iterations,
            'maker_agent': maker,
            'reviewer_agent': reviewer
        }
        return self

    def with_thread_history(self, history: List[Dict[str, str]]):
        """Set thread history for conversational mode"""
        self._context['thread_history'] = history
        self._context['conversation_mode'] = 'threaded'
        return self

    def build(self) -> Dict[str, Any]:
        """Build the task context dictionary"""
        return self._context
