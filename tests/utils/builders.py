"""
Test builders for creating test data structures

Provides fluent interfaces for building complex test objects
without verbose setup code in every test.
"""

import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import ClassVar, List, Dict, Any, Optional
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


class JoinResult(Enum):
    """What join_all() found. Falsy for everything but a clean join.

    NONE_REGISTERED is the member that earns this type: it means the patch did
    not take, which an exception-or-nothing signature reports as success.
    """
    ALL_JOINED = "all_joined"
    NONE_REGISTERED = "none_registered"
    STUCK = "stuck"

    def __bool__(self) -> bool:
        return self is JoinResult.ALL_JOINED


class RecordedThread:
    """A `threading.Thread` stand-in that records instead of running.

    `services/project_monitor.py::_start_review_cycle_for_issue` ends by
    spawning a daemon thread that runs a REAL review cycle. Two test files
    drive that method past its pipeline-lock gate to assert on the gate, and
    both used to let the thread actually start: it kept running into whatever
    test came next, connecting to Redis, writing to Elasticsearch and taking
    file locks under `state/projects/<project>/...` while unrelated tests were
    asserting (#186). That path was CWD-relative at the time, which is what
    made it land in the deployment; services/review_cycle.py resolves it
    properly now (#181), but the thread leak is the same either way. Nondeterministic timing, which makes
    it exactly the kind of thing that produces chunk-dependent results.

    Patch it in where the gate is exercised:

        RecordedThread.reset()
        with patch('threading.Thread', RecordedThread):
            monitor._start_review_cycle_for_issue(...)   # the ONE spawning call
        assert RecordedThread.started() == 1

    The patch is GLOBAL and there is no narrower option. `services/project_
    monitor.py` does its `import threading` inside functions, so there is no
    `services.project_monitor.threading` attribute to patch -- and adding a
    module-level import would not help either, because that attribute IS the
    `threading` module, so patching through it sets `threading.Thread` exactly
    as the global form does. Measured, not assumed.

    So the mitigation is the WINDOW, not the target: wrap only the call that
    spawns. A wide window also replaces `ThreadPoolExecutor`'s internal
    `threading.Thread(...)`, whose workers become no-ops -- the first
    `future.result()` then blocks forever, and there is no per-test timeout in
    this project to interrupt it (see pytest.ini).

    Note the parentheses on `started()`: it is a classmethod, so `assert
    RecordedThread.started` asserts a bound method object and can never fail.

    The gate's own behaviour is unchanged -- it still constructs a thread and
    calls start() -- so what the test is actually checking is not weakened.
    """

    instances: ClassVar[List["RecordedThread"]] = []

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None,
                 *, daemon=None):
        # Signature mirrors threading.Thread's exactly, including the unused
        # leading `group`. An earlier version reordered it, so a positional
        # `Thread(None, fn)` bound fn to `daemon` and recorded a thread with no
        # target -- silently, because it also swallowed unknown kwargs in
        # **extra where the real Thread raises TypeError.
        self.group = group
        self.target = target
        self.daemon = daemon
        self.args = args
        self.kwargs = kwargs or {}
        self.name = name if name is not None else f'recorded-{len(RecordedThread.instances)}'
        self.did_start = False
        RecordedThread.instances.append(self)

    def start(self):
        self.did_start = True

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False

    @classmethod
    def reset(cls):
        cls.instances.clear()

    @classmethod
    def started(cls) -> int:
        return sum(1 for t in cls.instances if t.did_start)


class JoinableThread(threading.Thread):
    """A real `threading.Thread` that a test can find again and join.

    `RecordedThread` is for tests asserting on a gate -- they do not want the
    body to run. A few tests do: they are testing what the thread's closure can
    see. Those still must not let it outlive them, and
    `_start_review_cycle_for_issue` does not return its thread, so there is
    nothing to join without this.

        JoinableThread.reset()
        with patch('threading.Thread', JoinableThread):
            monitor._start_review_cycle_for_issue(...)   # the ONE spawning call
        assert JoinableThread.join_all() is JoinResult.ALL_JOINED

    Global patch, narrow window -- see RecordedThread for why there is no
    narrower target. The hazard here differs from RecordedThread's: this class
    subclasses Thread and does NOT override run(), so an executor's workers
    still run. What goes wrong instead is that they REGISTER, under names like
    `ThreadPoolExecutor-0_0` that match no entry in
    PERSISTENT_POOL_THREAD_PREFIXES, and join_all() then waits out its whole
    budget on a session-lifetime worker and reports STUCK against the wrong
    test.
    """

    instances: ClassVar[List["JoinableThread"]] = []

    def start(self):
        # Registered on START, not in __init__. Thread.join() raises
        # RuntimeError on a thread that was constructed but never started, and
        # that raise used to happen inside join_all()'s loop -- before its own
        # reset -- leaving stale entries for the NEXT test's join_all() to fail
        # on, for an unrelated reason.
        JoinableThread.instances.append(self)
        super().start()

    @classmethod
    def reset(cls):
        cls.instances.clear()

    @classmethod
    def join_all(cls, timeout: float = 5.0) -> "JoinResult":
        """Join every thread this class started, within ONE shared budget.

        Returns rather than raises, because "nothing was registered" and
        "everything joined cleanly" are different answers and the first one is
        the dangerous one: if the patch silently stops taking -- a module
        switches to `from threading import Thread`, the spawn moves behind a
        helper -- an empty registry read as success means the test that most
        needs to fail is the one that passes. This is the same collapse
        CommitResult and TouchResult were introduced to remove in production,
        so it gets the same treatment, `__bool__` included.

        One budget for the whole call, not `timeout` per thread: N stuck
        threads would otherwise cost N x timeout serially. See
        tests/unit/services/test_agent_container_recovery_commit_join_budget.py
        for the production bug of exactly that shape.

        Threads belonging to the process-global pools are skipped rather than
        joined -- they are session-lifetime workers by design, and waiting on
        one to exit is waiting forever.
        """
        from tests.conftest import PERSISTENT_POOL_THREAD_PREFIXES

        deadline = time.monotonic() + timeout
        stuck = []
        joined = 0
        for thread in list(cls.instances):
            if thread.name.startswith(PERSISTENT_POOL_THREAD_PREFIXES):
                continue
            remaining = max(0.05, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if thread.is_alive():
                stuck.append(thread.name)
            else:
                joined += 1

        if stuck:
            # Registry deliberately NOT cleared: these references are the only
            # handle on threads that are still running, and this class exists
            # because the production code does not return its thread. The
            # conftest leak guard reports the survivors independently.
            return JoinResult.STUCK

        cls.reset()
        return JoinResult.ALL_JOINED if joined else JoinResult.NONE_REGISTERED
