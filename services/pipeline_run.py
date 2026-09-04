"""
Pipeline Run Management for Orchestrator Observability

Tracks the lifecycle of an issue's journey through the workflow pipeline.
All observability events and logs are tagged with pipeline_run_id for traceability.
"""

import asyncio
import logging
import redis
import json
import uuid
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass, asdict, fields
from elasticsearch import Elasticsearch
from monitoring.observability import es_index_with_retry

logger = logging.getLogger(__name__)


def format_pipeline_run_issue_key(project: str, issue_number: int, board: Optional[str] = None) -> str:
    """Redis hash field for the issue->pipeline_run_id mapping.

    board=None produces the legacy (pre-board-scoping) 2-field format.
    get_active_pipeline_run() now checks the board-scoped key first when it's
    given a board, falling back to this legacy key — see its own docstring —
    so board=None is no longer "out of scope" for it, just its default when
    no board is known. Existing callers that still omit board are unaffected:
    they get exactly this legacy-key behavior, unchanged. Pass board whenever
    it's known — it disambiguates runs for the same (project, issue_number)
    active on different boards.
    """
    if board:
        return f"{project}:{board}:{issue_number}"
    return f"{project}:{issue_number}"


# Atomically delete a hash field only if its current value matches — used by
# _cleanup_issue_mapping() to remove a legacy-format issue-mapping entry
# without racing a concurrent writer (e.g. get_active_pipeline_run()'s
# ES-restore path) that may have just pointed it at a different run.
# KEYS[1] = hash name, ARGV[1] = field, ARGV[2] = expected value
_COMPARE_AND_DELETE_HASH_FIELD_SCRIPT = """
if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
    return redis.call('HDEL', KEYS[1], ARGV[1])
else
    return 0
end
"""

# ILM Policy for pipeline runs (7-day retention)
PIPELINE_RUNS_ILM_POLICY = {
    "policy": {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "set_priority": {
                        "priority": 100
                    }
                }
            },
            "warm": {
                "min_age": "3d",
                "actions": {
                    "set_priority": {
                        "priority": 50
                    }
                }
            },
            "delete": {
                "min_age": "7d",
                "actions": {
                    "delete": {
                        "delete_searchable_snapshot": True
                    }
                }
            }
        }
    }
}

# Index template for pipeline runs
PIPELINE_RUNS_TEMPLATE = {
    "index_patterns": ["pipeline-runs-*"],
    "template": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "index": {
                "lifecycle": {
                    "name": "pipeline-runs-ilm-policy"
                }
            }
        },
        "mappings": {
            "properties": {
                "id": {"type": "keyword"},
                "issue_number": {"type": "integer"},
                "issue_title": {"type": "text"},
                "issue_url": {"type": "keyword"},
                "project": {"type": "keyword"},
                "board": {"type": "keyword"},
                "started_at": {"type": "date"},
                "ended_at": {"type": "date"},
                "status": {"type": "keyword"},
                "outcome": {"type": "keyword"}
            }
        }
    },
    "priority": 200
}


@dataclass
class PipelineRun:
    """
    Represents a single run of an issue through the workflow pipeline

    A pipeline run starts when an agent is about to be launched for an issue
    and ends when the issue reaches a column with no agent defined.
    """
    id: str
    issue_number: int
    issue_title: str
    issue_url: str
    project: str
    board: str
    started_at: str
    ended_at: Optional[str] = None
    status: str = "active"  # active, feedback_listening, completed
    discussion_id: Optional[str] = None  # GitHub discussion node ID for context continuity
    outcome: Optional[str] = None  # success, failed, or None for unknown
    context_dir: Optional[str] = None  # Path to pipeline context directory on host volume
    branch_name: Optional[str] = None  # Git branch resolved for this run by resolve_workspace() (issue #120)
    project_dir: Optional[str] = None  # Epic worktree path resolved by resolve_workspace() (issue #120) -- resolve_workspace() always creates/adopts an epic worktree for the workspace types it handles; never a shared base-clone path
    epic_id: Optional[str] = None  # Epic id resolve_workspace() scoped project_dir's worktree by (issue #121 review) -- lets callers read it directly instead of reverse-engineering it from project_dir's path layout

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PipelineRun':
        """Create from dictionary, ignoring unknown fields (e.g. analysis fields written back by pipeline_run_analysis)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def is_active(self) -> bool:
        """Check if pipeline run is still active (executing or waiting for human feedback)"""
        return self.status in ("active", "feedback_listening") and self.ended_at is None


class PipelineRunManager:
    """
    Manages pipeline run lifecycle with dual storage:
    - Redis: Fast access for active pipeline runs
    - Elasticsearch: Historical persistence for analysis
    """
    
    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        elasticsearch_client: Optional[Elasticsearch] = None
    ):
        """
        Initialize pipeline run manager
        
        Args:
            redis_client: Redis client for fast access
            elasticsearch_client: Elasticsearch client for persistence
        """
        # Redis for fast active run lookups
        if redis_client:
            self.redis = redis_client
        else:
            self.redis = redis.Redis(
                host='redis',
                port=6379,
                decode_responses=True
            )
        
        # Elasticsearch for historical persistence
        self.es = elasticsearch_client
        if elasticsearch_client is None:
            try:
                self.es = Elasticsearch(["http://elasticsearch:9200"])
            except Exception as e:
                logger.warning(f"Failed to connect to Elasticsearch: {e}")
                self.es = None
        
        # Redis key prefix
        self.redis_prefix = "orchestrator:pipeline_run"
        self.redis_issue_mapping = "orchestrator:pipeline_run:issue_mapping"

        # Elasticsearch index pattern (date-based for ILM)
        self.es_index_pattern = "pipeline-runs"

        # Setup Elasticsearch ILM and templates if available
        if self.es:
            self._setup_elasticsearch()

        logger.info("PipelineRunManager initialized")

    def _setup_elasticsearch(self):
        """Setup Elasticsearch ILM policy and index templates"""
        if not self.es:
            return

        try:
            # Create ILM policy for pipeline runs (7-day retention)
            self.es.ilm.put_lifecycle(
                name="pipeline-runs-ilm-policy",
                body=PIPELINE_RUNS_ILM_POLICY
            )
            logger.info("Created/updated ILM policy: pipeline-runs-ilm-policy (7-day retention)")

            # Create index template for pipeline runs
            self.es.indices.put_index_template(
                name="pipeline-runs-template",
                body=PIPELINE_RUNS_TEMPLATE
            )
            logger.info("Created/updated index template: pipeline-runs-template")

        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch for pipeline runs: {e}")

    def _get_es_index_name(self, date: Optional[datetime] = None) -> str:
        """
        Get date-based Elasticsearch index name for pipeline runs

        Args:
            date: Optional date to use for index name (defaults to today)

        Returns:
            Index name like 'pipeline-runs-2025-11-05'
        """
        if date is None:
            date = datetime.utcnow()
        return f"{self.es_index_pattern}-{date.strftime('%Y-%m-%d')}"

    def _get_redis_key(self, pipeline_run_id: str) -> str:
        """Get Redis key for pipeline run"""
        return f"{self.redis_prefix}:{pipeline_run_id}"
    
    def _get_issue_key(self, project: str, issue_number: int, board: Optional[str] = None) -> str:
        """Get Redis hash field for issue mapping"""
        return format_pipeline_run_issue_key(project, issue_number, board)

    def _cleanup_issue_mapping(self, project: str, issue_number: int, board: Optional[str], pipeline_run_id: str) -> None:
        """Remove this run's issue-mapping entries: the board-scoped key it was
        stored under, and — defensively — the legacy 2-field key too, but only
        if it still points at THIS run_id.

        Both deletes are compare-and-delete, not unconditional HDEL: a new run
        for this exact (project, board, issue_number) could in principle be
        created concurrently (e.g. get_or_create_pipeline_run's duplicate
        guard racing this cleanup) between resolving this run and this call —
        an unconditional HDEL on the board-scoped key would then wipe out that
        new run's freshly-written mapping instead of this (ending) run's own.

        Why the legacy key needs checking at all: get_active_pipeline_run()'s
        ES fallback restores into the board-scoped key when it was given a
        board, but still restores into the legacy key otherwise (its default
        when no board is known — see its own docstring). Without this check, a
        run ending would leave that legacy entry orphaned forever (no other
        cleanup job exists for it), so any future legacy-format lookup for
        this issue would keep
        returning this now-completed run's ID indefinitely. Comparing the
        value before deleting avoids clobbering a DIFFERENT board's still-
        active run that might currently occupy that same legacy slot.
        """
        board_key = self._get_issue_key(project, issue_number, board)
        try:
            self.redis.eval(
                _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT,
                1,
                self.redis_issue_mapping,
                board_key,
                pipeline_run_id,
            )
        except Exception as e:
            logger.warning(f"Failed to clean up issue mapping {board_key}: {e}")

        legacy_key = self._get_issue_key(project, issue_number)
        if legacy_key != board_key:
            try:
                # Atomic compare-and-delete via EVAL — a separate HGET-then-HDEL
                # has a real TOCTOU window: get_active_pipeline_run()'s ES-restore
                # path can write a DIFFERENT board's run into this same legacy
                # key between the check and the delete, and an unconditional
                # HDEL at that point would wipe out that other board's freshly-
                # restored, still-active mapping.
                self.redis.eval(
                    _COMPARE_AND_DELETE_HASH_FIELD_SCRIPT,
                    1,
                    self.redis_issue_mapping,
                    legacy_key,
                    pipeline_run_id,
                )
            except Exception as e:
                logger.warning(f"Failed to clean up legacy issue mapping {legacy_key}: {e}")
    
    def create_pipeline_run(
        self,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        project: str,
        board: str,
        discussion_id: Optional[str] = None
    ) -> PipelineRun:
        """
        Create a new pipeline run

        Args:
            issue_number: GitHub issue number
            issue_title: Issue title
            issue_url: Issue URL
            project: Project name
            board: Board name
            discussion_id: Optional GitHub discussion node ID for context continuity

        Returns:
            New PipelineRun instance
        """
        pipeline_run_id = str(uuid.uuid4())

        pipeline_run = PipelineRun(
            id=pipeline_run_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_url=issue_url,
            project=project,
            board=board,
            started_at=datetime.utcnow().isoformat() + 'Z',
            status="active",
            discussion_id=discussion_id
        )
        
        # Store in Redis for fast access
        redis_key = self._get_redis_key(pipeline_run_id)
        self.redis.setex(
            redis_key,
            7200,  # 2 hour TTL
            json.dumps(pipeline_run.to_dict())
        )
        
        # Map issue to pipeline run ID
        issue_key = self._get_issue_key(project, issue_number, board)
        self.redis.hset(
            self.redis_issue_mapping,
            issue_key,
            pipeline_run_id
        )
        
        # Persist to Elasticsearch
        self._persist_to_elasticsearch(pipeline_run)
        
        logger.info(
            f"Created pipeline run {pipeline_run_id} for "
            f"{project} issue #{issue_number}"
        )

        return pipeline_run

    def _persist_run_update(self, pipeline_run: 'PipelineRun') -> None:
        """
        Shared write-through: save the run's current full state to Redis and
        Elasticsearch. Extracted (code review, issue #120) so update_context_dir()
        and update_resolved_workspace() -- which both save the whole run after a
        single field changes -- share one persistence path instead of drifting
        independently. create_pipeline_run()'s own initial-write copy is left
        separate (a bigger, separately-established method, out of this item's scope).
        """
        redis_key = self._get_redis_key(pipeline_run.id)
        self.redis.setex(
            redis_key,
            7200,  # 2 hour TTL
            json.dumps(pipeline_run.to_dict())
        )
        self._persist_to_elasticsearch(pipeline_run)

    def update_context_dir(self, pipeline_run: 'PipelineRun') -> None:
        """
        Persist an updated context_dir field back to Redis and Elasticsearch.

        Called after the pipeline context directory is created so that subsequent
        task lookups (including feedback tasks) can find the directory path.
        """
        self._persist_run_update(pipeline_run)
        logger.debug(
            f"Updated context_dir for pipeline run {pipeline_run.id}: {pipeline_run.context_dir}"
        )

    def update_resolved_workspace(self, pipeline_run: 'PipelineRun') -> None:
        """
        Persist resolved branch_name/project_dir fields back to Redis and Elasticsearch.

        Called by resolve_workspace() once the epic branch/worktree decision is made,
        so a later idempotent call of resolve_workspace() (or any other lookup) for
        the same run sees the persisted values instead of re-resolving.
        """
        self._persist_run_update(pipeline_run)
        logger.debug(
            f"Updated resolved workspace for pipeline run {pipeline_run.id}: "
            f"branch_name={pipeline_run.branch_name}, project_dir={pipeline_run.project_dir}"
        )

    async def resolve_workspace(
        self,
        pipeline_run: 'PipelineRun',
        github_integration,
        workspace_type: str,
    ) -> 'PipelineRun':
        """
        Resolve (and persist) the git branch and isolated epic worktree this
        pipeline run's work should land on.

        Purely additive infrastructure (issue #120, WI-A of #119): builds the single
        canonical branch/worktree decision meant to eventually replace the two
        independent algorithms in use today -- FeatureBranchManager.
        find_related_branches() (ordinary 'issues'/'hybrid' dispatch's
        prepare_feature_branch(), checking out on the shared base clone) and
        resolve_epic_branch_name()/get_or_create_epic_worktree() (the repair-cycle
        path in project_monitor.py). Not yet called from any production dispatch
        path -- see #119 for the follow-up work items that wire it in.

        Scoped to 'issues'/'hybrid' workspace types only; any other workspace_type
        is a no-op that returns pipeline_run unchanged ('discussions' is git-free).

        Idempotent: once pipeline_run.branch_name and pipeline_run.project_dir are
        both already set (e.g. a second call against an already-resolved run),
        returns immediately without touching git or GitHub again.

        Resolution order:
        1. Resolve the epic id. For workspace_type == 'issues', matches
           project_monitor.py's _resolve_epic_worktree_target() exactly, including its
           hard-failure behavior: sdlc_execution's 'issues' sub-issues always have a
           parent epic by construction, so a missing parent is a hard ValueError (NOT
           resolve_epic_id()'s lenient fallback to the issue's own number -- silently
           scoping isolation to the wrong number would defeat the whole point). For
           workspace_type == 'hybrid', there is no equivalent precedent guaranteeing a
           parent (e.g. a directly-dispatched epic or a standalone issue can legitimately
           have none), so this uses FeatureBranchManager.resolve_epic_id()'s existing,
           already-established fallback instead of inventing a hard-fail this workspace
           type has never required.
        2. Look up an already-established branch for that epic via
           resolve_epic_branch_name() (cache, then a git branch listing keyed on
           the epic id) -- covers the exact-issue, parent-issue, and cached match
           tiers of find_related_branches() in one lookup, since epic_id already
           IS the number those tiers match against.
        3. If nothing exists yet, the single authoritative fallback name:
           create_feature_branch_name(). find_related_branches()'s
           semantic-similarity guess (its 4th, weakest tier) is deliberately not
           consulted here.
        4. Get-or-create the epic's isolated worktree for that branch via
           ProjectWorkspaceManager.get_or_create_epic_worktree().

        Args:
            pipeline_run: The run to resolve a workspace for. Mutated in place
                with branch_name/project_dir/epic_id and saved back to Redis/Elasticsearch.
            github_integration: GitHubIntegration instance used to resolve the
                parent issue (see FeatureBranchManager.get_parent_issue).
            workspace_type: The dispatch's workspace type ('issues', 'hybrid',
                'discussions', ...). Only 'issues'/'hybrid' are resolved.

        Returns:
            pipeline_run, with branch_name/project_dir/epic_id populated for 'issues'/
            'hybrid' runs (unchanged for any other workspace_type).

        Raises:
            ValueError: For workspace_type == 'issues', no parent epic issue could be
                resolved (see step 1 above) -- this is deliberately ambiguous between
                "genuinely no parent" and "the GitHub API call failed/errored", since
                FeatureBranchManager.get_parent_issue() itself doesn't distinguish those
                cases; check its own logs for the underlying cause before assuming this
                issue truly has no parent. Also raised if the project has no base clone
                to source the worktree from.
            RuntimeError: The underlying git worktree add command failed.

        This method deliberately does not swallow either exception itself -- a caller
        wiring this into real dispatch must let them reach whatever failure handling
        keeps a pipeline run/lock from getting stuck (the bug an earlier version of this
        logic, project_monitor.py's _resolve_epic_worktree_target(), was fixed for after
        leaving pipeline locks stuck forever on an unhandled failure). That does NOT
        require distinguishing ValueError from RuntimeError, or handling either
        specially: propagating both to one generic outer exception handler -- this
        codebase's established uniform retry/escalation pattern (see
        count_consecutive_failures()/MAX_CONSECUTIVE_DISPATCH_FAILURES), rather than
        special-casing errors that merely look permanent for immediate termination -- is
        a correct, intended way to satisfy this contract, and is what this method's
        current production caller does.
        """
        if workspace_type not in ('issues', 'hybrid'):
            return pipeline_run

        if pipeline_run.branch_name and pipeline_run.project_dir and pipeline_run.epic_id:
            logger.debug(
                f"Workspace already resolved for pipeline run {pipeline_run.id}: "
                f"branch_name={pipeline_run.branch_name}, "
                f"project_dir={pipeline_run.project_dir}, epic_id={pipeline_run.epic_id}"
            )
            return pipeline_run

        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        if workspace_type == 'issues':
            # sdlc_execution's 'issues' sub-issues always have a parent epic by
            # construction -- matches _resolve_epic_worktree_target()'s identical
            # hard-fail for exactly this workspace_type. Deliberately NOT
            # resolve_epic_id(): silently scoping isolation to this sub-issue's own
            # number instead of its real epic would defeat the whole point.
            parent_issue = await feature_branch_manager.get_parent_issue(
                github_integration, pipeline_run.issue_number, project=pipeline_run.project
            )
            if not parent_issue:
                raise ValueError(
                    f"Pipeline run {pipeline_run.id} ({pipeline_run.project}/#{pipeline_run.issue_number}, "
                    f"workspace_type={workspace_type!r}) could not resolve a parent epic issue via GitHub's "
                    "structured sub-issue API -- either it genuinely has none, or the API call itself "
                    "failed/errored (get_parent_issue() doesn't distinguish the two; check its logs). "
                    "'issues' dispatch must have a parent to scope an isolated worktree by -- silently "
                    "falling back to this issue's own number would defeat cross-sub-issue isolation. "
                    "Matches project_monitor.py's _resolve_epic_worktree_target()."
                )
            epic_id = str(parent_issue)
        else:
            # 'hybrid': no equivalent precedent guarantees a parent (a directly-
            # dispatched epic or a standalone issue can legitimately have none) --
            # use the same established, lenient fallback every other existing
            # caller of resolve_epic_id() already relies on.
            epic_id = await feature_branch_manager.resolve_epic_id(
                github_integration, pipeline_run.issue_number, project=pipeline_run.project
            )

        # resolve_epic_branch_name() and get_or_create_epic_worktree() are both
        # synchronous and can do blocking git subprocess work (a branch listing;
        # git fetch/worktree add for a cold epic). Run them off the event loop
        # thread so they can't stall other coroutines scheduled on it -- same
        # precaution agent_executor.py's near-identical resolution already takes,
        # for the same reason.
        branch_name = await asyncio.to_thread(
            feature_branch_manager.resolve_epic_branch_name, pipeline_run.project, epic_id
        )
        if not branch_name:
            branch_name = feature_branch_manager.create_feature_branch_name(int(epic_id), "")

        project_dir = await asyncio.to_thread(
            workspace_manager.get_or_create_epic_worktree, pipeline_run.project, epic_id, branch_name
        )

        # get_or_create_epic_worktree() can silently adopt a pre-existing worktree that's
        # actually on a different branch than branch_name requested (e.g. after an
        # orchestrator restart with an empty in-memory cache -- see its own "Adopting
        # pre-existing epic worktree" warning). It logs that mismatch internally but only
        # returns a bare Path, not the branch it actually settled on. Re-derive the real
        # branch directly from the worktree, using the same best-effort check
        # get_or_create_epic_worktree() itself uses internally, rather than trusting the
        # locally-resolved branch_name blindly -- persisting a branch_name that doesn't
        # match what's actually checked out at project_dir would silently mis-target any
        # later git push/PR-base operation that trusts this field.
        actual_branch = await asyncio.to_thread(
            workspace_manager._current_worktree_branch, project_dir
        )
        if actual_branch and actual_branch != branch_name:
            logger.warning(
                f"Resolved branch_name={branch_name!r} for pipeline run {pipeline_run.id} "
                f"but the epic worktree at {project_dir} is actually on {actual_branch!r} -- "
                "persisting the worktree's real branch instead."
            )
            branch_name = actual_branch

        pipeline_run.branch_name = branch_name
        pipeline_run.project_dir = str(project_dir)
        pipeline_run.epic_id = epic_id
        try:
            self.update_resolved_workspace(pipeline_run)
        except Exception:
            # Revert the in-memory mutation on a persistence failure -- otherwise a
            # caller that catches this and retries resolve_workspace() against the
            # SAME pipeline_run object hits the idempotency guard above (both fields
            # already look set) and returns immediately without ever actually
            # persisting, silently losing the resolution forever.
            pipeline_run.branch_name = None
            pipeline_run.project_dir = None
            pipeline_run.epic_id = None
            raise

        logger.info(
            f"Resolved workspace for pipeline run {pipeline_run.id} "
            f"({pipeline_run.project} epic #{pipeline_run.epic_id}): branch_name={branch_name}, "
            f"project_dir={pipeline_run.project_dir}"
        )
        return pipeline_run

    def get_recent_pipeline_run_id(
        self,
        project: str,
        issue_number: int,
        board: Optional[str] = None
    ) -> Optional[str]:
        """
        Get the most recent pipeline_run_id for an issue, regardless of status.

        Read-only: does NOT restore Redis state or modify any mappings.
        Use for event attribution when the run may already be completed
        (e.g. post-completion PR-ready checks, parent issue advancement).

        Args:
            project: Project name
            issue_number: Issue number
            board: Optional board name. When given, both the Redis lookup and
                the ES fallback are scoped to this board, disambiguating runs
                for the same (project, issue_number) active on different
                boards. When omitted, behavior is unchanged from before board
                scoping existed: the legacy 2-field Redis key is checked, and
                the ES query is unfiltered by board — callers that explicitly
                want "any recent run for this issue" regardless of board.

        Returns:
            pipeline_run_id string if any run exists for this issue, None otherwise
        """
        # 1. Check the issue→run mapping (covers active runs)
        issue_key = self._get_issue_key(project, issue_number, board)
        run_id = self.redis.hget(self.redis_issue_mapping, issue_key)
        if run_id:
            return run_id

        # 2. Check ES for the most recent run (any status)
        if self.es:
            try:
                must_clauses = [
                    {"term": {"project": project}},
                    {"term": {"issue_number": issue_number}}
                ]
                if board:
                    must_clauses.append({"term": {"board": board}})
                result = self.es.search(
                    index=f"{self.es_index_pattern}-*",
                    body={
                        "query": {
                            "bool": {
                                "must": must_clauses
                            }
                        },
                        "size": 1,
                        "sort": [{"started_at": {"order": "desc"}}],
                        "_source": ["id"]
                    }
                )
                if result['hits']['total']['value'] > 0:
                    return result['hits']['hits'][0]['_source']['id']
            except Exception as e:
                logger.debug(f"Error searching ES for recent pipeline run: {e}")

        return None

    def get_active_pipeline_run(
        self,
        project: str,
        issue_number: int,
        board: Optional[str] = None
    ) -> Optional[PipelineRun]:
        """
        Get active pipeline run for an issue

        Args:
            project: Project name
            issue_number: Issue number
            board: Optional board name. create_pipeline_run() always writes the
                board-scoped Redis mapping (see _get_issue_key), so when board
                is given here that key is checked FIRST, falling back to the
                legacy board-less key for runs restored from before board
                scoping existed. When board is omitted, only the legacy key is
                checked — identical to this method's behavior before board
                support was added, so existing callers are unaffected.

                Without passing board, a run created moments earlier by
                get_or_create_pipeline_run() (which always writes the
                board-scoped key) can be invisible to a second, same-second
                lookup for the same (project, issue_number) — producing a
                duplicate "phantom" pipeline run instead of reusing the one
                that already exists.

        Returns:
            PipelineRun if active run exists, None otherwise
        """
        # Check issue mapping — board-scoped key first (if board given), then
        # the legacy key. When board is None (the common, pre-existing case) the
        # list comprehension below contributes nothing, so exactly one key (the
        # legacy one) is checked — same as before board support was added.
        # dict.fromkeys() is defensive scaffolding, not load-bearing: the two key
        # formats ("project:board:issue" vs "project:issue") can never collide.
        issue_keys = list(dict.fromkeys([
            *([self._get_issue_key(project, issue_number, board)] if board else []),
            self._get_issue_key(project, issue_number),
        ]))

        for issue_key in issue_keys:
            pipeline_run_id = self.redis.hget(self.redis_issue_mapping, issue_key)
            if not pipeline_run_id:
                continue

            # Fast path: mapping exists, try Redis first
            redis_key = self._get_redis_key(pipeline_run_id)
            data = self.redis.get(redis_key)

            if data:
                try:
                    pipeline_run = PipelineRun.from_dict(json.loads(data))
                    if not pipeline_run.is_active():
                        # Run has completed — clean up stale mapping
                        self.redis.hdel(self.redis_issue_mapping, issue_key)
                        continue
                    if board and pipeline_run.board and pipeline_run.board != board:
                        # The board-scoped key always matches by construction; this
                        # guards the LEGACY key, which is shared across boards. Its
                        # mapping can point at a different board's genuinely-active
                        # run for this (project, issue_number) — e.g. left behind by
                        # a board=None caller, or backfilled by this method's own ES
                        # restore path below. Adopting that run here would be wrong:
                        # everything downstream (end_pipeline_run's lock release,
                        # queue processing, etc.) keys off pipeline_run.board, not
                        # the board this caller asked for. Treat it as a miss.
                        logger.debug(
                            f"get_active_pipeline_run: legacy key for {project} issue "
                            f"#{issue_number} points at run {pipeline_run.id} on board "
                            f"'{pipeline_run.board}', not requested board '{board}' — skipping"
                        )
                        continue
                    return pipeline_run
                except Exception as e:
                    # Don't leave a corrupted mapping in place — every subsequent
                    # call for this issue would otherwise re-hit this same error and
                    # never self-heal (it only ever falls through to the ES fallback).
                    logger.error(
                        f"Error deserializing pipeline run {pipeline_run_id} for {project} "
                        f"issue #{issue_number} (key={issue_key}): {e} — removing corrupted mapping"
                    )
                    self.redis.hdel(self.redis_issue_mapping, issue_key)
                    continue
            else:
                # Redis data has expired (TTL) while the issue mapping survived.
                # This can happen if the orchestrator was restarted and the initial
                # 2-hour creation TTL expired before the run was restored/updated.
                # Clean up the stale mapping and fall through to the next key / ES lookup.
                logger.debug(
                    f"Pipeline run data for {project} issue #{issue_number} expired from Redis "
                    f"(run_id: {pipeline_run_id[:8]}...), falling back to Elasticsearch"
                )
                self.redis.hdel(self.redis_issue_mapping, issue_key)
                # Fall through to try the next key / ES lookup below

        # ES fallback: mapping was absent OR Redis data expired.
        # Include "feedback_listening" so human-feedback loops survive restarts
        # without being recreated (they legitimately have no Docker container).
        if self.es:
            try:
                must_clauses = [
                    {"term": {"project": project}},
                    {"term": {"issue_number": issue_number}},
                    {"terms": {"status": ["active", "feedback_listening"]}}
                ]
                if board:
                    # Board given: filter strictly by it. Every PipelineRun doc has a
                    # board field (it's a required create_pipeline_run() arg), so this
                    # correctly disambiguates instead of risking a match against a
                    # DIFFERENT board's genuinely-active run for the same issue number.
                    must_clauses.append({"term": {"board": board}})
                result = self.es.search(
                    index=f"{self.es_index_pattern}-*",
                    body={
                        "query": {"bool": {"must": must_clauses}},
                        "size": 1,
                        "sort": [{"started_at": {"order": "desc"}}]
                    }
                )

                if result['hits']['total']['value'] > 0:
                    pipeline_run = PipelineRun.from_dict(result['hits']['hits'][0]['_source'])
                    logger.debug(f"Found active pipeline run {pipeline_run.id} in Elasticsearch (not in Redis)")

                    # Guard against ES near-real-time stale reads.
                    # end_pipeline_run() stores completed data in Redis (setex) and deletes
                    # the issue mapping (hdel) BEFORE persisting to ES.  If the ES search
                    # index hasn't refreshed yet, this query can return a stale "active"
                    # document for a run that was just completed.  Restoring that stale
                    # data would overwrite the correct completed state in Redis and cause
                    # subsequent callers to reuse the old pipeline_run_id.
                    check_key = self._get_redis_key(pipeline_run.id)
                    existing_data = self.redis.get(check_key)
                    if existing_data:
                        try:
                            existing = json.loads(existing_data)
                            # "failed" is just as terminal as "completed" here — both
                            # mean end_pipeline_run() already ran for this ID, so a
                            # stale ES "active" doc must not be allowed to resurrect it.
                            if existing.get('status') in ('completed', 'failed'):
                                logger.info(
                                    f"Skipping ES fallback restore for {pipeline_run.id} — "
                                    f"already {existing.get('status')} in Redis (stale ES read)"
                                )
                                return None
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # Restore to Redis. feedback_listening runs get a 7-day TTL to cover
                    # any realistic human review window; active runs get a 1-hour refresh.
                    restore_key = self._get_redis_key(pipeline_run.id)
                    if pipeline_run.status == 'feedback_listening':
                        self.redis.setex(restore_key, 604800, json.dumps(pipeline_run.to_dict()))
                    else:
                        self.redis.setex(restore_key, 3600, json.dumps(pipeline_run.to_dict()))
                    # Restore under the board-scoped key when board was given (matches
                    # what create_pipeline_run() writes), else the legacy key — NOT
                    # whatever `issue_key` last held from the Redis loop above.
                    restore_issue_key = self._get_issue_key(project, issue_number, board)
                    self.redis.hset(self.redis_issue_mapping, restore_issue_key, pipeline_run.id)

                    return pipeline_run
            except Exception as e:
                logger.debug(f"Error searching Elasticsearch for active pipeline run: {e}")

        return None
    
    def get_or_create_pipeline_run(
        self,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        project: str,
        board: str,
        discussion_id: Optional[str] = None
    ) -> tuple['PipelineRun', bool]:
        """
        Get existing active pipeline run or create a new one

        This method ensures that only ONE active run exists per issue by:
        1. Checking Redis for an active run
        2. If not in Redis, querying Elasticsearch for any active runs
        3. Ending any old active runs found in Elasticsearch
        4. Creating a new run if needed

        Args:
            issue_number: GitHub issue number
            issue_title: Issue title
            issue_url: Issue URL
            project: Project name
            board: Board name
            discussion_id: Optional GitHub discussion node ID for context continuity

        Returns:
            Tuple of (PipelineRun instance, was_created) where was_created=True means
            a new run was just created, False means an existing active run was returned.
        """
        # Use a lock to prevent race conditions when creating runs
        lock_key = f"{self.redis_prefix}:lock:{project}:{issue_number}"
        
        try:
            # Try to acquire lock (wait up to 5 seconds)
            with self.redis.lock(lock_key, timeout=5, blocking_timeout=5):
                # Check for existing active run in Redis. Pass board — this is the
                # actual root-cause fix for the phantom-duplicate-run bug: without
                # it, a run this same method just created (which always writes the
                # board-scoped key) is invisible to a second, same-second call for
                # the same (project, issue_number), so it creates a duplicate
                # instead of reusing the one that already exists.
                existing = self.get_active_pipeline_run(project, issue_number, board=board)

                if existing:
                    if existing.status == 'feedback_listening':
                        # A different trigger is reusing this run — the feedback loop that
                        # set it to feedback_listening is no longer the one driving it (the
                        # human answered and a new stage/agent picked the issue up). Restore
                        # to "active" so the dashboard doesn't keep showing "awaiting
                        # feedback" indefinitely for a run someone else now owns.
                        logger.info(
                            f"Reusing feedback_listening pipeline run {existing.id} for "
                            f"{project} issue #{issue_number} via new trigger — restoring to active"
                        )
                        self.update_run_status(project, issue_number, 'active', board=board)
                        existing.status = 'active'
                    logger.debug(
                        f"Using existing pipeline run {existing.id} for "
                        f"{project} issue #{issue_number}"
                    )
                    return existing, False
                
                # Redis doesn't have an active run, but Elasticsearch might have old ones.
                # Two cases:
                #   1. "feedback_listening" runs: the human feedback loop is still active
                #      (or was active before a restart).  Restore to Redis and RETURN as-is —
                #      do NOT end them; the feedback loop will call end_pipeline_run() itself.
                #   2. "active" orphan runs: the orchestrator restarted and nobody owns them.
                #      End them so a fresh run can be created.
                if self.es:
                    try:
                        query = {
                            "query": {
                                "bool": {
                                    "must": [
                                        {"term": {"project": project}},
                                        {"term": {"issue_number": issue_number}},
                                        {"terms": {"status": ["active", "feedback_listening"]}}
                                    ]
                                }
                            },
                            "size": 100,
                            "sort": [{"started_at": {"order": "desc"}}]
                        }

                        result = self.es.search(index="pipeline-runs-*", body=query)

                        if result['hits']['total']['value'] > 0:
                            feedback_run_to_reuse = None
                            for hit in result['hits']['hits']:
                                old_run_data = hit['_source']
                                old_run_id = old_run_data['id']
                                old_status = old_run_data.get('status', 'active')

                                if old_status == 'feedback_listening' and feedback_run_to_reuse is None:
                                    # Keep the most-recent feedback-listening run to reuse;
                                    # continue iterating to also clean up any orphaned "active" runs.
                                    logger.info(
                                        f"Found feedback_listening pipeline run {old_run_id} for "
                                        f"{project} issue #{issue_number} — will restore to Redis and reuse"
                                    )
                                    feedback_run_to_reuse = (old_run_data, hit)
                                elif old_status == 'feedback_listening':
                                    # Duplicate feedback_listening run (shouldn't happen) — end it
                                    logger.warning(
                                        f"Ending duplicate feedback_listening pipeline run {old_run_id} for "
                                        f"{project} issue #{issue_number}"
                                    )
                                    old_run_data['ended_at'] = datetime.utcnow().isoformat() + 'Z'
                                    old_run_data['status'] = 'completed'
                                    es_index_with_retry(self.es, hit['_index'], old_run_data, doc_id=old_run_id)
                                else:
                                    # Orphaned "active" run — end it
                                    logger.warning(
                                        f"Ending orphaned active pipeline run {old_run_id} for "
                                        f"{project} issue #{issue_number}"
                                    )
                                    old_run_data['ended_at'] = datetime.utcnow().isoformat() + 'Z'
                                    old_run_data['status'] = 'completed'
                                    old_index = hit['_index']
                                    es_index_with_retry(self.es, old_index, old_run_data, doc_id=old_run_id)

                            if feedback_run_to_reuse is not None:
                                run_data, _ = feedback_run_to_reuse
                                pipeline_run = PipelineRun.from_dict(run_data)
                                redis_key = self._get_redis_key(pipeline_run.id)
                                self.redis.setex(redis_key, 3600, json.dumps(pipeline_run.to_dict()))
                                self.redis.hset(self.redis_issue_mapping, self._get_issue_key(project, issue_number, board), pipeline_run.id)
                                # Same reasoning as the Redis fast-path above — a new trigger
                                # is reusing a feedback_listening run restored from ES, so
                                # restore its status to "active" as well.
                                logger.info(
                                    f"Reusing feedback_listening pipeline run {pipeline_run.id} for "
                                    f"{project} issue #{issue_number} via new trigger (ES fallback) — restoring to active"
                                )
                                self.update_run_status(project, issue_number, 'active', board=board)
                                pipeline_run.status = 'active'
                                return pipeline_run, False
                    except Exception as e:
                        logger.error(f"Error checking/ending old pipeline runs in Elasticsearch: {e}")
                
                # Clear any stale cancellation signal from a previous pipeline run.
                # clear() handles its own Redis errors; this outer catch guards against import failures.
                try:
                    from services.cancellation import get_cancellation_signal
                    get_cancellation_signal().clear(project, issue_number)
                except Exception as e:
                    logger.error(
                        f"Failed to clear stale cancellation signal for {project} issue #{issue_number}: {e}. "
                        f"Agents dispatched for this pipeline run may be immediately cancelled."
                    )

                # Create new run
                new_run = self.create_pipeline_run(
                    issue_number=issue_number,
                    issue_title=issue_title,
                    issue_url=issue_url,
                    project=project,
                    board=board,
                    discussion_id=discussion_id
                )
                return new_run, True
        except redis.exceptions.LockError:
            logger.warning(f"Could not acquire lock for pipeline run creation: {project} #{issue_number}")
            # Fallback: try to get existing one last time
            existing = self.get_active_pipeline_run(project, issue_number, board=board)
            if existing:
                if existing.status == 'feedback_listening':
                    logger.info(
                        f"Reusing feedback_listening pipeline run {existing.id} for "
                        f"{project} issue #{issue_number} via new trigger (no-lock fallback) — restoring to active"
                    )
                    self.update_run_status(project, issue_number, 'active', board=board)
                    existing.status = 'active'
                return existing, False
            
            # If we can't get lock and no existing run, proceed with creation anyway
            # This is a best-effort fallback
            logger.warning("Proceeding with pipeline run creation without lock")
            try:
                from services.cancellation import get_cancellation_signal
                get_cancellation_signal().clear(project, issue_number)
            except Exception as e:
                logger.error(
                    f"Failed to clear stale cancellation signal for {project} issue #{issue_number}: {e}. "
                    f"Agents dispatched for this pipeline run may be immediately cancelled."
                )
            new_run = self.create_pipeline_run(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_url=issue_url,
                project=project,
                board=board
            )
            return new_run, True

    def get_pipeline_run(self, pipeline_run_id: str) -> Optional['PipelineRun']:
        """
        Get a pipeline run by ID from Redis or Elasticsearch.

        This method is used to recover pipeline runs that may have been
        created before a restart, checking both Redis (for recent runs)
        and Elasticsearch (for older runs).

        Args:
            pipeline_run_id: The ID of the pipeline run to retrieve

        Returns:
            PipelineRun if found, None otherwise
        """
        # Try Redis first (fast path for recent runs < 2 hours old)
        redis_key = f"orchestrator:pipeline_run:{pipeline_run_id}"
        try:
            run_data = self.redis.get(redis_key)
            if run_data:
                return PipelineRun(**json.loads(run_data))
        except Exception as e:
            logger.error(f"Failed to query Redis for pipeline run {pipeline_run_id}: {e}")

        # Fallback to Elasticsearch (slow path, for runs > 2 hours old)
        if self.es:
            try:
                result = self.es.search(
                    index="pipeline-runs-*",
                    body={
                        "query": {
                            "bool": {
                                "must": [
                                    {"term": {"id": pipeline_run_id}}
                                ]
                            }
                        },
                        "size": 1
                    }
                )

                if result['hits']['total']['value'] > 0:
                    hit = result['hits']['hits'][0]['_source']
                    return PipelineRun(**hit)
            except Exception as e:
                logger.error(f"Failed to query Elasticsearch for pipeline run {pipeline_run_id}: {e}")

        return None

    def ensure_pipeline_run_for_task(
        self,
        project: str,
        board: str,
        issue_number: int,
        issue_data: Optional[Dict[str, Any]] = None,
        discussion_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Ensure a pipeline run exists for a task being created.

        Convenience method for queue processing paths that need to create/retrieve
        pipeline run before task dispatch. Uses get_or_create_pipeline_run() under
        the hood, so it returns existing run if one is active.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number
            issue_data: Optional issue data. If not provided, uses fallback values.
            discussion_id: Optional GitHub discussion node ID for context continuity

        Returns:
            Pipeline run ID if successful, None if failed
        """
        try:
            # Clear any stale cancellation signal from a previous pipeline run
            try:
                from services.cancellation import get_cancellation_signal
                get_cancellation_signal().clear(project, issue_number)
            except Exception as e:
                logger.warning(f"Failed to clear stale cancellation signal: {e}")

            # Extract issue metadata
            if issue_data:
                issue_title = issue_data.get('title', f'Issue #{issue_number}')
                issue_url = issue_data.get('url', f'https://github.com/unknown/{issue_number}')
            else:
                # Fallback if issue data not provided
                issue_title = f'Issue #{issue_number}'
                issue_url = f'https://github.com/unknown/{issue_number}'
                logger.debug(
                    f"No issue data provided for pipeline run creation, using fallback"
                )

            # Get or create pipeline run (returns existing if active)
            pipeline_run, _ = self.get_or_create_pipeline_run(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_url=issue_url,
                project=project,
                board=board,
                discussion_id=discussion_id
            )

            logger.debug(
                f"Ensured pipeline run {pipeline_run.id} for {project} issue #{issue_number}"
            )

            return pipeline_run.id

        except Exception as e:
            logger.error(
                f"Failed to ensure pipeline run for {project} issue #{issue_number}: {e}"
            )
            return None

    def update_run_status(
        self,
        project: str,
        issue_number: int,
        new_status: str,
        board: Optional[str] = None,
    ) -> bool:
        """
        Update the status of an active pipeline run without ending it.

        Used to transition a run to "feedback_listening" when the human feedback
        loop starts monitoring, so the zombie watchdog (which only queries for
        status="active") does not kill the run.

        Args:
            project: Project name
            issue_number: Issue number
            new_status: New status value (e.g. "feedback_listening")
            board: Optional board name, passed through to get_active_pipeline_run()
                so a board-scoped-only run can still be found. Omit to preserve
                prior legacy-key-only behavior.

        Returns:
            True if the run was updated, False if no active run was found.
        """
        pipeline_run = self.get_active_pipeline_run(project, issue_number, board=board)
        if not pipeline_run:
            logger.debug(
                f"update_run_status: no active run found for {project} issue #{issue_number}"
            )
            return False

        pipeline_run.status = new_status

        # Persist to Redis
        try:
            redis_key = self._get_redis_key(pipeline_run.id)
            if new_status == 'feedback_listening':
                # 7-day TTL — long enough for any realistic human review window.
                # Prevents indefinite accumulation if the run is abandoned without being ended.
                self.redis.setex(redis_key, 604800, json.dumps(pipeline_run.to_dict()))
            else:
                self.redis.setex(redis_key, 3600, json.dumps(pipeline_run.to_dict()))
        except Exception as e:
            logger.warning(f"Failed to update pipeline run status in Redis: {e}")

        # Persist to Elasticsearch (use the same index the run was originally written to)
        if self.es:
            try:
                index_name = self._get_es_index_name()
                if pipeline_run.started_at:
                    try:
                        started_date = datetime.fromisoformat(
                            pipeline_run.started_at.replace('Z', '+00:00')
                        )
                        index_name = self._get_es_index_name(started_date)
                    except Exception:
                        pass
                es_index_with_retry(self.es, index_name, pipeline_run.to_dict(), doc_id=pipeline_run.id)
            except Exception as e:
                logger.warning(f"Failed to update pipeline run status in Elasticsearch: {e}")

        logger.info(
            f"Pipeline run {pipeline_run.id} for {project} issue #{issue_number} "
            f"status → {new_status}"
        )
        return True

    def mark_failed(
        self,
        project: str,
        board: str,
        issue_number: int,
        reason: str,
    ) -> bool:
        """
        The single shared entry point for every pipeline-failure path: ends any
        active PipelineRun for this issue with outcome="failed" (for rich ES/
        dashboard history within its retention window), and unconditionally,
        durably marks the pipeline lock as retained-due-to-failure regardless of
        whether a PipelineRun happened to exist for this attempt.

        The lock mark is the part that actually blocks re-dispatch (see
        PipelineLockManager.mark_lock_failed/get_retained_reason) — it does NOT
        depend on the PipelineRun call succeeding, because some failure paths (e.g.
        repeated dispatch failures before an agent ever successfully launched) can
        legitimately have no PipelineRun to end yet, and the caller is always
        guaranteed to already hold the lock at the point this is called.

        This replaces the old services.work_execution_state halt-marker mechanism
        (set_halt_marker/get_halt_marker/clear_halt_marker), which stored its flag
        in a per-issue YAML file with no relationship to the lock, no visibility,
        and no expiry-safe reconciliation.

        Returns:
            True if the lock was actually durably marked (the part that matters
            for enforcement), False if it wasn't. mark_lock_failed() reports its
            own failure via return value, not exceptions (empty reason, no lock
            held, or both Redis and YAML writes failing are all reported this
            way) — callers MUST check this return value rather than assume
            success, since posting a "the lock is retained" comment or log line
            when it actually isn't would itself be a silent-failure regression.
        """
        try:
            self.end_pipeline_run(
                project=project,
                board=board,
                issue_number=issue_number,
                reason=reason,
                retain_lock=True,
                outcome="failed",
            )
        except Exception as e:
            logger.error(
                f"mark_failed: end_pipeline_run raised for {project} issue "
                f"#{issue_number}: {e}"
            )

        # Unconditional fallback/guarantee: end_pipeline_run() above only touches the
        # lock if it found an active PipelineRun. Always mark it directly too so a
        # pre-dispatch failure (no PipelineRun ever created for this attempt) is
        # covered just as reliably as a mid-execution crash. This is also the
        # authoritative result — mark_lock_failed is idempotent, so calling it
        # again here even when end_pipeline_run already succeeded is harmless,
        # and this is the only call that reports its actual outcome back to us.
        try:
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            return get_pipeline_lock_manager().mark_lock_failed(project, board, issue_number, reason)
        except Exception as e:
            logger.error(
                f"mark_failed: could not durably mark lock for {project} issue "
                f"#{issue_number}: {e}"
            )
            return False

    def end_pipeline_run(
        self,
        project: str,
        issue_number: int,
        reason: Optional[str] = None,
        retain_lock: Optional[bool] = None,
        outcome: Optional[str] = None,
        board: Optional[str] = None
    ) -> bool:
        """
        End an active pipeline run

        Args:
            project: Project name
            issue_number: Issue number
            reason: Optional reason for ending (for logging)
            retain_lock: Lock policy after the run ends.
                - None (default): auto — retain if outcome="failed", release otherwise.
                - True: always retain regardless of outcome.
                - False: always release regardless of outcome (use for intentional kills).
            board: Optional board name, passed through to get_active_pipeline_run()
                so a board-scoped-only run (e.g. an orphaned "phantom" run created
                by get_or_create_pipeline_run() and never reused) can still be
                found and ended. Omit to preserve prior legacy-key-only behavior.

        Returns:
            True if run was ended, False if no active run found
        """
        # Get active run
        pipeline_run = self.get_active_pipeline_run(project, issue_number, board=board)
        
        if not pipeline_run:
            logger.debug(
                f"No active pipeline run to end for {project} issue #{issue_number}"
            )
            return False
        
        # Mark as completed (or "failed" — a distinct terminal status, not just an
        # outcome flag on an otherwise-indistinguishable "completed" record, so
        # visibility surfaces like /active-pipeline-runs can find failed runs by
        # status instead of missing them entirely — see observability_server.py)
        pipeline_run.ended_at = datetime.utcnow().isoformat() + 'Z'
        pipeline_run.status = "failed" if outcome == "failed" else "completed"
        pipeline_run.outcome = outcome

        # Set cancellation signal so in-flight repair cycles stop.
        # Skip for feedback_loop_ended — that reason indicates a conversational loop
        # exiting normally (e.g., stop requested, backlog). Setting the signal here
        # would race against the next column's loop starting and cancel it immediately.
        # Deliberately reason-string-based rather than a caller-supplied override
        # flag: every current failure path (mark_failed()'s callers) durably
        # retains the lock, which blocks any "next loop" this signal could
        # spuriously race against — so a failure path never has a legitimate
        # reason to suppress it, and no such flag currently has any real caller
        # (round 5 briefly added one for a case this reasoning shows didn't
        # need it; round 6 removed it rather than carry unused API forward).
        _effective_reason = reason or "completed"
        if _effective_reason != "feedback_loop_ended":
            try:
                from services.cancellation import get_cancellation_signal
                get_cancellation_signal().cancel(
                    project, issue_number,
                    f"Pipeline run ended: {_effective_reason}"
                )
            except Exception as e:
                logger.warning(f"Failed to set cancellation signal on pipeline end: {e}")

        # Emit pipeline completion event BEFORE updating Redis
        # This ensures observability matches state transitions
        try:
            from monitoring.observability import get_observability_manager, EventType
            obs = get_observability_manager()
            obs.emit(
                EventType.PIPELINE_RUN_COMPLETED,
                "pipeline_lifecycle",
                pipeline_run.id,
                project,
                {
                    "pipeline_run_id": pipeline_run.id,
                    "issue_number": issue_number,
                    "board": pipeline_run.board,
                    "started_at": pipeline_run.started_at,
                    "ended_at": pipeline_run.ended_at,
                    "reason": reason or "completed",
                    "duration_seconds": (
                        datetime.fromisoformat(pipeline_run.ended_at.rstrip('Z')) -
                        datetime.fromisoformat(pipeline_run.started_at.rstrip('Z'))
                    ).total_seconds()
                },
                pipeline_run_id=pipeline_run.id
            )
            logger.info(f"Emitted pipeline_run_completed event for {project} issue #{issue_number}")
        except Exception as e:
            logger.error(f"Failed to emit pipeline completion event: {e}", exc_info=True)
            # Continue with state update even if event emission fails

        # Update Redis
        redis_key = self._get_redis_key(pipeline_run.id)
        self.redis.setex(
            redis_key,
            3600,  # Keep for 1 hour after completion
            json.dumps(pipeline_run.to_dict())
        )
        
        # Remove from issue mapping (can't be reused)
        self._cleanup_issue_mapping(project, issue_number, pipeline_run.board, pipeline_run.id)

        # Update in Elasticsearch
        self._persist_to_elasticsearch(pipeline_run)

        # Release pipeline lock if this issue holds it.
        # retain_lock=True  → always retain. Historically this meant "caller
        #     wants manual intervention", but pipeline_watchdog.py's zombie/
        #     frozen-run self-heal (see incident e42ca133) also passes True
        #     for an AUTOMATIC retry: it relies on retain_lock=True + this
        #     method's outcome="failed" branch below durably marking the lock
        #     via mark_lock_failed(), then immediately calls
        #     PipelineLockManager.clear_retained_reason() itself to lift that
        #     mark for its own self-heal window — so "retain_lock=True" no
        #     longer implies "requires a human", only "the lock must not be
        #     released to the general pool by end_pipeline_run itself".
        # retain_lock=False → always release (intentional kill / cleanup)
        # retain_lock=None  → auto: retain on failure, release on success/other
        should_retain = (
            retain_lock is True
            or (retain_lock is None and outcome == "failed")
        )
        if should_retain:
            if outcome == "failed":
                logger.warning(
                    f"Retaining pipeline lock for {project} issue #{issue_number} — "
                    f"run ended with outcome=failed. Manual intervention required to release."
                )
                # Durably mark the lock itself (Redis + non-expiring YAML) as retained
                # due to failure. This is the enforcement signal every dispatch gate
                # consults — see PipelineLockManager.mark_lock_failed/get_retained_reason.
                # Deliberately NOT gated on `should_retain` alone (retain_lock=True
                # without outcome="failed" is used for other intentional-retain cases
                # that should NOT permanently block re-dispatch), only on genuine
                # failure.
                try:
                    from services.pipeline_lock_manager import get_pipeline_lock_manager
                    marked_ok = get_pipeline_lock_manager().mark_lock_failed(
                        project, pipeline_run.board, issue_number,
                        reason=reason or "Pipeline run failed",
                    )
                    # mark_lock_failed reports its own failure via return value,
                    # not exceptions (empty reason, no lock held, or both Redis
                    # and YAML writes failing are all reported this way — see its
                    # docstring). end_pipeline_run() is called from ~15 sites
                    # across the codebase that don't go through the mark_failed()
                    # wrapper (which does check this), so this is the only place
                    # that would ever notice a failure for most of them — log it
                    # loudly rather than let it pass as if retention succeeded.
                    if not marked_ok:
                        logger.critical(
                            f"end_pipeline_run: could NOT durably mark the lock "
                            f"failed for {project} issue #{issue_number} — the "
                            f"run is recorded as failed but the lock may not "
                            f"actually be retained. This issue may be silently "
                            f"re-dispatched."
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to durably mark lock as failed for {project} issue "
                        f"#{issue_number}: {e}"
                    )
            else:
                logger.info(
                    f"Retaining pipeline lock for {project} issue #{issue_number} after ending run "
                    f"(retain_lock=True, reason: {reason or 'unspecified'})"
                )
            return True
        try:
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            lock_manager = get_pipeline_lock_manager()
            current_lock = lock_manager.get_lock(project, pipeline_run.board)
            if current_lock and current_lock.lock_status == 'locked' and current_lock.locked_by_issue == issue_number:
                # Does NOT force — this branch only runs when should_retain is
                # False (an intentional non-retaining end), but the lock could
                # still independently be retained from an unrelated prior
                # failure. release_lock() correctly refuses in that case; don't
                # proceed to dispatch the next queued issue as if this one
                # cleanly released.
                released = lock_manager.release_lock(project, pipeline_run.board, issue_number)
                if not released:
                    logger.error(
                        f"Could not release pipeline lock for {project} issue "
                        f"#{issue_number} after ending run — it is likely retained "
                        f"due to an unrelated failure. Not dispatching the next "
                        f"queued issue while this is unresolved."
                    )
                    return True
                logger.info(f"Released pipeline lock for {project} issue #{issue_number} after ending run")

                # CRITICAL: Process next waiting issue in queue after lock release
                # This ensures queued issues are picked up when the current issue completes
                try:
                    from services.pipeline_queue_manager import get_pipeline_queue_manager
                    from task_queue.task_manager import Task, TaskPriority
                    import time

                    pipeline_queue = get_pipeline_queue_manager(project, pipeline_run.board)
                    next_issue = pipeline_queue.get_next_waiting_issue()
                    
                    if next_issue:
                        logger.info(f"Attempting to acquire lock for next queued issue #{next_issue['issue_number']} after #{issue_number} completed")
                        
                        # Try to acquire lock for next issue
                        acquired, acquire_reason = lock_manager.try_acquire_lock(
                            project=project,
                            board=pipeline_run.board,
                            issue_number=next_issue['issue_number']
                        )
                        
                        if acquired:
                            # CRITICAL: Mark issue active IMMEDIATELY after lock acquisition
                            # This prevents monitoring loop from seeing "issue has lock" and creating duplicate task
                            # The monitoring loop checks if issue holds lock (line 1507 in project_monitor.py)
                            # If yes, it assumes work is resuming and proceeds to create task
                            # We must mark active BEFORE that check can happen
                            pipeline_queue.mark_issue_active(next_issue['issue_number'])
                            logger.info(f"Successfully acquired lock for issue #{next_issue['issue_number']}")
                            
                            # CRITICAL: Actually dispatch the agent by creating a task
                            # Not sufficient to just acquire lock - need to enqueue task
                            # SAFETY: Track task_created for rollback if creation fails
                            task_created = False
                            try:
                                from config.manager import ConfigManager
                                config_manager = ConfigManager()
                                project_config = config_manager.get_project_config(project)
                                pipeline_config = next(p for p in project_config.pipelines if p.board_name == pipeline_run.board)
                                workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)
                                
                                # SAFETY: Re-fetch issue from GitHub to verify it hasn't moved columns
                                # The queue cache might be stale if user moved the issue
                                # FIX: Use GraphQL query instead of gh issue view --json projectItems
                                # because projectItems can be stale/empty due to GitHub eventual consistency
                                actual_column = self._get_issue_column_from_github(
                                    project_config, pipeline_config, next_issue['issue_number']
                                )

                                if not actual_column:
                                    raise Exception(f"Issue #{next_issue['issue_number']} not found on board '{pipeline_run.board}'")
                                
                                # Get agent for ACTUAL current column (not cached column)
                                agent = None
                                for col in workflow_template.columns:
                                    if col.name == actual_column:
                                        agent = col.agent
                                        break
                                
                                if agent and agent != 'null':
                                    # CRITICAL: Get or create pipeline run for next queued issue
                                    # Ensures work is tracked under a pipeline run from the start
                                    try:
                                        # Fetch issue details for pipeline run (need title and URL)
                                        import subprocess
                                        result = subprocess.run(
                                            ['gh', 'issue', 'view', str(next_issue['issue_number']),
                                             '--repo', f"{project_config.github['org']}/{project_config.github['repo']}",
                                             '--json', 'title,body,url'],
                                            capture_output=True, text=True, check=True
                                        )
                                        next_issue_data = json.loads(result.stdout)
                                    except Exception as e:
                                        logger.warning(
                                            f"Could not fetch issue details for #{next_issue['issue_number']}: {e}"
                                        )
                                        next_issue_data = None

                                    # Get or create pipeline run for next issue
                                    pipeline_run_id = self.ensure_pipeline_run_for_task(
                                        project=project,
                                        board=pipeline_run.board,
                                        issue_number=next_issue['issue_number'],
                                        issue_data=next_issue_data
                                    )

                                    if not pipeline_run_id:
                                        raise Exception(
                                            f"Failed to create/retrieve pipeline run for issue #{next_issue['issue_number']}"
                                        )

                                    # Create task for next issue with ACTUAL column (not cached)
                                    task_context = {
                                        'project': project,
                                        'board': pipeline_run.board,
                                        'pipeline': pipeline_config.name,
                                        'repository': project_config.github['repo'],
                                        'issue_number': next_issue['issue_number'],
                                        'issue': next_issue_data,  # ADD THIS
                                        'column': actual_column,  # Use verified actual column
                                        'trigger': 'lock_release_queue_processing',
                                        'pipeline_run_id': pipeline_run_id,  # ADD THIS
                                        'timestamp': datetime.utcnow().isoformat() + 'Z'
                                    }
                                    
                                    # Instantiate TaskQueue using the correct import
                                    from task_queue.task_manager import TaskQueue
                                    task_queue = TaskQueue(use_redis=True)

                                    task = Task(
                                        id=f"{agent}_{project}_{pipeline_run.board}_{next_issue['issue_number']}_{int(time.time())}",
                                        agent=agent,
                                        project=project,
                                        priority=TaskPriority.MEDIUM,
                                        context=task_context,
                                        created_at=datetime.utcnow().isoformat() + 'Z'
                                    )

                                    task_queue.enqueue(task)
                                    task_created = True
                                    
                                    logger.info(
                                        f"Dispatched agent {agent} for next queued issue #{next_issue['issue_number']} "
                                        f"in column '{actual_column}'"
                                    )
                                else:
                                    raise Exception(
                                        f"Next queued issue #{next_issue['issue_number']} in column '{actual_column}' "
                                        f"has no agent configured"
                                    )
                            except Exception as dispatch_error:
                                # CRITICAL: Rollback lock acquisition if task creation failed
                                # Otherwise lock is held with no work happening (deadlock)
                                if not task_created:
                                    logger.error(
                                        f"Task creation failed for issue #{next_issue['issue_number']}, "
                                        f"rolling back lock acquisition to prevent deadlock"
                                    )
                                    try:
                                        lock_manager.release_lock(project, pipeline_run.board, next_issue['issue_number'])
                                        logger.info(f"Rolled back lock for issue #{next_issue['issue_number']}")
                                    except Exception as rollback_error:
                                        logger.error(f"Failed to rollback lock: {rollback_error}")
                                
                                logger.error(f"Error dispatching agent for next issue: {dispatch_error}")
                                import traceback
                                logger.error(traceback.format_exc())
                        else:
                            logger.info(
                                f"Could not acquire lock for next issue #{next_issue['issue_number']}: {acquire_reason}"
                            )
                    else:
                        logger.debug(f"No more issues waiting in queue for {project}/{pipeline_run.board}")
                except Exception as queue_error:
                    logger.error(f"Error processing next queued issue for {project}/{pipeline_run.board}: {queue_error}")
                    import traceback
                    logger.error(traceback.format_exc())
                    
        except Exception as e:
            logger.warning(f"Failed to release pipeline lock for {project} issue #{issue_number}: {e}")
        
        reason_msg = f" ({reason})" if reason else ""
        logger.info(
            f"Ended pipeline run {pipeline_run.id} for "
            f"{project} issue #{issue_number}{reason_msg}"
        )

        return True

    def end_phantom_pipeline_run(
        self,
        pipeline_run_id: str,
        project: str,
        issue_number: int,
        reason: Optional[str] = None,
    ) -> bool:
        """
        End one specific pipeline run by id — for cleaning up a "phantom" run
        that was superseded by a DIFFERENT run for the same (project,
        issue_number) before it ever became the one driving execution (e.g.
        trigger_agent_for_status() eagerly creates a run, then discovers the
        column is a pr_review stage and _start_pr_review_for_issue() creates
        or reuses a run of its own).

        Deliberately does NOT go through end_pipeline_run()'s (project,
        issue_number[, board]) active-run lookup: by the time this is called,
        the issue-mapping may already point at the run that superseded this
        one, so that lookup could resolve to — and incorrectly end — the
        WRONG, currently-active run instead of this phantom. Fetching
        directly by id avoids that.

        Also deliberately skips everything end_pipeline_run() does around the
        pipeline lock, the cancellation signal, and the wait queue. This is NOT
        because the phantom predates some other run's "lock-acquisition step" —
        neither PipelineLock nor CancellationSignal is keyed by pipeline_run_id
        at all, so there is no such thing as "the phantom's lock" vs. "the real
        run's lock" to begin with:
          - PipelineLock (services/pipeline_lock_manager.py) is keyed by
            (project, board) — every issue on that board shares the one lock,
            so there isn't even a per-issue lock, let alone a per-run one.
          - CancellationSignal (services/cancellation.py) is keyed by
            (project, issue_number), independent of board.
        Either way, a phantom and the run that superseded it — same project,
        same board, same issue — always resolve to the exact same lock and the
        exact same cancellation signal. Touching either from here would affect
        whichever execution currently owns that issue's lock — phantom or real
        — so this method never calls into lock/signal/queue code at all; only
        the run object itself (Redis/ES bookkeeping and the issue-mapping
        cleanup) is touched.

        Returns True if an active run with this id was found and ended,
        False if it was not found or was already terminal (nothing to do).
        """
        pipeline_run = self.get_pipeline_run_by_id(pipeline_run_id)
        if not pipeline_run or not pipeline_run.is_active():
            logger.debug(
                f"end_phantom_pipeline_run: no active run found for id {pipeline_run_id}"
            )
            return False

        pipeline_run.ended_at = datetime.utcnow().isoformat() + 'Z'
        pipeline_run.status = "completed"
        pipeline_run.outcome = "superseded"

        try:
            from monitoring.observability import get_observability_manager, EventType
            obs = get_observability_manager()
            obs.emit(
                EventType.PIPELINE_RUN_COMPLETED,
                "pipeline_lifecycle",
                pipeline_run.id,
                project,
                {
                    "pipeline_run_id": pipeline_run.id,
                    "issue_number": issue_number,
                    "board": pipeline_run.board,
                    "started_at": pipeline_run.started_at,
                    "ended_at": pipeline_run.ended_at,
                    "reason": reason or "superseded",
                    "duration_seconds": (
                        datetime.fromisoformat(pipeline_run.ended_at.rstrip('Z')) -
                        datetime.fromisoformat(pipeline_run.started_at.rstrip('Z'))
                    ).total_seconds()
                },
                pipeline_run_id=pipeline_run.id
            )
        except Exception as e:
            logger.error(
                f"Failed to emit pipeline completion event for phantom run {pipeline_run_id}: {e}",
                exc_info=True
            )

        redis_key = self._get_redis_key(pipeline_run.id)
        self.redis.setex(redis_key, 3600, json.dumps(pipeline_run.to_dict()))

        # Compare-and-delete: only removes the issue mapping if it still points
        # at THIS run id, so a real run created/restored concurrently for the
        # same (project, board, issue_number) can't have its own freshly-written
        # mapping clobbered by this phantom's cleanup.
        self._cleanup_issue_mapping(project, issue_number, pipeline_run.board, pipeline_run.id)

        self._persist_to_elasticsearch(pipeline_run)

        logger.info(
            f"Ended phantom pipeline run {pipeline_run.id} for {project} issue #{issue_number}"
            f"{f' ({reason})' if reason else ''}"
        )
        return True

    def get_pipeline_run_by_id(self, pipeline_run_id: str) -> Optional[PipelineRun]:
        """
        Get pipeline run by ID (from Redis or Elasticsearch)

        Args:
            pipeline_run_id: Pipeline run ID

        Returns:
            PipelineRun if found, None otherwise
        """
        # Try Redis first
        redis_key = self._get_redis_key(pipeline_run_id)
        data = self.redis.get(redis_key)

        if data:
            try:
                return PipelineRun.from_dict(json.loads(data))
            except Exception as e:
                logger.error(f"Error deserializing pipeline run from Redis: {e}")

        # Fall back to Elasticsearch (search across all date-based indices)
        if self.es:
            try:
                # Use search instead of get to query across date-based indices
                result = self.es.search(
                    index=f"{self.es_index_pattern}-*",
                    body={
                        "query": {
                            "term": {
                                "_id": pipeline_run_id
                            }
                        },
                        "size": 1
                    }
                )

                if result and result['hits']['total']['value'] > 0:
                    return PipelineRun.from_dict(result['hits']['hits'][0]['_source'])
            except Exception as e:
                logger.debug(f"Pipeline run {pipeline_run_id} not found in Elasticsearch: {e}")

        return None
    
    def _persist_to_elasticsearch(self, pipeline_run: PipelineRun):
        """
        Persist pipeline run to Elasticsearch (date-based index)

        Args:
            pipeline_run: PipelineRun to persist
        """
        if not self.es:
            return

        try:
            # Use date-based index name derived from started_at to ensure updates go to the same index
            index_name = self._get_es_index_name()
            try:
                if pipeline_run.started_at:
                    # Parse started_at (format: "2025-11-29T18:29:17.250Z" or similar)
                    started_at_str = pipeline_run.started_at.replace('Z', '+00:00')
                    started_date = datetime.fromisoformat(started_at_str)
                    index_name = self._get_es_index_name(started_date)
            except Exception as e:
                logger.warning(f"Could not parse started_at '{pipeline_run.started_at}', using current date for index: {e}")

            es_index_with_retry(self.es, index_name, pipeline_run.to_dict(), doc_id=pipeline_run.id)
            logger.debug(f"Persisted pipeline run {pipeline_run.id} to {index_name}")
        except Exception as e:
            logger.error(f"Failed to persist pipeline run to Elasticsearch: {e}")
    
    def cleanup_expired_mappings(self, max_age_seconds: int = 7200):
        """
        Clean up expired pipeline run mappings from Redis
        
        This is a maintenance function that should be called periodically.
        It removes stale mappings where the pipeline run data no longer exists.
        
        Args:
            max_age_seconds: Maximum age in seconds before cleanup
        """
        try:
            # Get all issue mappings
            all_mappings = self.redis.hgetall(self.redis_issue_mapping)
            
            cleaned = 0
            for issue_key, pipeline_run_id in all_mappings.items():
                redis_key = self._get_redis_key(pipeline_run_id)
                
                # Check if pipeline run data still exists
                if not self.redis.exists(redis_key):
                    self.redis.hdel(self.redis_issue_mapping, issue_key)
                    cleaned += 1
            
            if cleaned > 0:
                logger.info(f"Cleaned up {cleaned} expired pipeline run mappings")
                
        except Exception as e:
            logger.error(f"Error cleaning up pipeline run mappings: {e}")
    
    def cleanup_stale_active_runs_on_startup(self, retriggered_issues: set = None):
        """
        Clean up stale 'active' pipeline runs on orchestrator startup.
        
        On startup, we need to:
        1. Query Elasticsearch for all 'active' pipeline runs
        2. Check if each issue is actually in a column with an agent
        3. End runs for issues in Done/Backlog or columns without agents
        4. Keep runs active only if they're in columns with agents assigned
        
        This fixes the issue where runs remain 'active' after:
        - Orchestrator restarts
        - Issues manually moved to Done
        - Issues moved to Backlog
        
        Args:
            retriggered_issues: Set of (project, issue_number) tuples for issues
                that were just re-triggered during lock recovery. These should
                NOT be cleaned up since they're about to start work.
        """
        if not self.es:
            logger.warning("Elasticsearch not available, skipping stale pipeline run cleanup")
            return
        
        if retriggered_issues is None:
            retriggered_issues = set()
        
        try:
            from config.manager import config_manager
            import subprocess
            import json
            
            # Get all active pipeline runs from Elasticsearch
            query = {
                "query": {
                    "term": {"status": "active"}
                },
                "size": 1000  # Get all active runs
            }
            
            result = self.es.search(index=f"{self.es_index_pattern}-*", body=query)
            
            if result['hits']['total']['value'] == 0:
                logger.info("No active pipeline runs to clean up")
                return
            
            logger.info(f"Found {result['hits']['total']['value']} active pipeline runs, checking if they should be ended")
            
            ended_count = 0
            kept_active_count = 0
            
            for hit in result['hits']['hits']:
                run = hit['_source']
                original_index = hit['_index']
                pipeline_run_id = run['id']
                project = run['project']
                issue_number = run['issue_number']
                board = run['board']

                try:
                    # CRITICAL: Skip issues that were just re-triggered during lock recovery
                    # They have tasks queued but haven't started executing yet
                    if (project, issue_number) in retriggered_issues:
                        logger.info(
                            f"Skipping cleanup for {project} issue #{issue_number} "
                            f"(run {pipeline_run_id[:8]}...) - was just re-triggered during lock recovery"
                        )
                        kept_active_count += 1
                        continue

                    # CRITICAL: Skip issues with active feedback loops
                    # These are long-running conversational sessions that should keep their pipeline runs
                    try:
                        from services.human_feedback_loop import human_feedback_loop_executor

                        lk = human_feedback_loop_executor._loop_key(project, issue_number)
                        if lk in human_feedback_loop_executor.active_loops:
                            logger.info(
                                f"Skipping cleanup for pipeline run {pipeline_run_id[:8]}... - "
                                f"active feedback loop exists for {project} issue #{issue_number}"
                            )
                            kept_active_count += 1
                            continue
                    except Exception as e:
                        # During testing or if module not available, skip this check
                        logger.debug(f"Could not check active feedback loops: {e}")

                    # Also check Redis lock for conversational loops (backup check)
                    lock_key = f"orchestrator:conversational_loop:{project}:{issue_number}"
                    try:
                        if self.redis.exists(lock_key):
                            logger.info(
                                f"Skipping cleanup for pipeline run {pipeline_run_id[:8]}... - "
                                f"conversational loop lock exists for {project} issue #{issue_number}"
                            )
                            kept_active_count += 1
                            continue
                    except Exception as e:
                        # During testing or if Redis not available, skip this check
                        logger.debug(f"Could not check conversational loop lock: {e}")

                    # Get project config
                    project_config = config_manager.get_project_config(project)
                    if not project_config:
                        logger.warning(f"No config for project {project}, ending run {pipeline_run_id}")
                        self._end_run_in_elasticsearch(run, "Project config not found", original_index)
                        ended_count += 1
                        continue
                    
                    # Find pipeline config for this board
                    pipeline_config = next(
                        (p for p in project_config.pipelines if p.board_name == board),
                        None
                    )
                    
                    if not pipeline_config:
                        logger.warning(f"No pipeline config for board {board}, ending run {pipeline_run_id}")
                        self._end_run_in_elasticsearch(run, "Pipeline config not found", original_index)
                        ended_count += 1
                        continue
                    
                    # Get workflow template
                    workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)
                    
                    # Get current column for this issue from GitHub Projects v2
                    # We need to query the project board to see what column the issue is in
                    current_column = self._get_issue_column_from_github(
                        project_config, pipeline_config, issue_number
                    )
                    
                    if not current_column:
                        logger.warning(
                            f"Could not determine column for issue #{issue_number}, "
                            f"ending run {pipeline_run_id} (issue may have been removed from board)"
                        )
                        self._end_run_in_elasticsearch(run, "Issue not found on board", original_index)
                        ended_count += 1
                        continue
                    
                    # Check if this column has an agent assigned
                    column_config = next(
                        (c for c in workflow_template.columns if c.name == current_column),
                        None
                    )
                    
                    if not column_config:
                        logger.warning(
                            f"Column {current_column} not in workflow, "
                            f"ending run {pipeline_run_id}"
                        )
                        self._end_run_in_elasticsearch(run, f"Column '{current_column}' not in workflow", original_index)
                        ended_count += 1
                        continue
                    
                    # CRITICAL: Determine if run should be active based on GitHub issue status
                    # A pipeline run is active if and only if:
                    # 1. Issue is NOT in an exit column (Done, Staged, etc.)
                    # 2. Issue IS in a column with an agent assigned
                    
                    # Check if issue is in an exit column
                    exit_columns = getattr(workflow_template, 'pipeline_exit_columns', [])
                    is_in_exit_column = current_column in exit_columns
                    
                    # Check if column has an agent
                    has_agent = column_config.agent and column_config.agent != 'null'
                    
                    if is_in_exit_column:
                        # Issue reached completion - end the run
                        logger.info(
                            f"Issue #{issue_number} in exit column '{current_column}', "
                            f"ending run {pipeline_run_id}"
                        )
                        self._end_run_in_elasticsearch(
                            run, 
                            f"Issue in exit column '{current_column}'", 
                            original_index
                        )
                        ended_count += 1
                    elif not has_agent:
                        # Column has no agent (e.g., Backlog) - end the run
                        logger.info(
                            f"Issue #{issue_number} in column '{current_column}' with no agent, "
                            f"ending run {pipeline_run_id}"
                        )
                        self._end_run_in_elasticsearch(
                            run, 
                            f"Issue in column '{current_column}' with no agent", 
                            original_index
                        )
                        ended_count += 1
                    else:
                        # Issue is in a column with an agent - keep run active
                        # The GitHub issue status is the source of truth
                        logger.debug(
                            f"Issue #{issue_number} in column '{current_column}' with agent {column_config.agent}, "
                            f"keeping run {pipeline_run_id} active"
                        )
                        kept_active_count += 1
                    
                except Exception as e:
                    logger.error(
                        f"Error checking pipeline run {pipeline_run_id} for "
                        f"{project} issue #{issue_number}: {e}"
                    )
                    # Keep active on error to be safe
                    kept_active_count += 1
            
            logger.info(
                f"Pipeline run cleanup complete: ended {ended_count} stale runs, "
                f"kept {kept_active_count} runs active"
            )
            
        except Exception as e:
            logger.error(f"Error during stale pipeline run cleanup: {e}")
    
    # REMOVED: _verify_pipeline_run_is_active()
    # The old approach tried to infer pipeline state from timing signals (recent activity,
    # running containers, queued tasks). This was fundamentally flawed because:
    # 1. Timing-based checks created race conditions during startup
    # 2. The "10 minute activity window" was arbitrary and unreliable
    # 3. Container/queue checks didn't account for legitimate pauses (waiting for human feedback)
    #
    # NEW APPROACH: Use GitHub issue status as the single source of truth
    # A pipeline run is active if and only if the issue is in a column with an agent
    # and NOT in an exit column. This is simple, deterministic, and testable.
    
    def _get_issue_column_from_github(self, project_config, pipeline_config, issue_number: int) -> Optional[str]:
        """
        Query GitHub Projects v2 to get the current column for an issue
        
        Args:
            project_config: Project configuration
            pipeline_config: Pipeline configuration
            issue_number: Issue number to look up
            
        Returns:
            Column name if found, None otherwise
        """
        try:
            import subprocess
            import json
            
            # Extract project number from board name (e.g., "Development Pipeline" -> number from GitHub)
            # We need to query the organization's projects to find the right one
            org = project_config.github.get('org')
            repo = project_config.github.get('repo')
            board_name = pipeline_config.board_name
            
            # Get project number from state manager
            from config.state_manager import state_manager
            github_state = state_manager.load_project_state(project_config.name)
            
            if not github_state or not github_state.boards:
                logger.warning(f"No GitHub state for project {project_config.name}")
                return None
            
            # github_state.boards is a dict, not a list
            board_state = github_state.boards.get(board_name)
            if not board_state or not board_state.project_number:
                logger.warning(f"No project number for board {board_name}")
                return None
            
            project_number = board_state.project_number
            
            # Query GitHub Projects v2 API for this specific issue
            from services.github_owner_utils import get_owner_type
            
            owner_type = get_owner_type(org)
            if owner_type is None:
                logger.error(f"Cannot query project items - unable to determine owner type for '{org}'")
                return None
            
            # Build the correct query based on owner type
            if owner_type == 'user':
                query = f'''{{
                    user(login: "{org}") {{
                        projectV2(number: {project_number}) {{
                            items(first: 100) {{
                                nodes {{
                                    content {{
                                        ... on Issue {{
                                            number
                                        }}
                                    }}
                                    fieldValues(first: 10) {{
                                        nodes {{
                                            ... on ProjectV2ItemFieldSingleSelectValue {{
                                                name
                                                field {{
                                                    ... on ProjectV2SingleSelectField {{
                                                        name
                                                    }}
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}'''
            else:  # organization
                query = f'''{{
                    organization(login: "{org}") {{
                        projectV2(number: {project_number}) {{
                            items(first: 100) {{
                                nodes {{
                                    content {{
                                        ... on Issue {{
                                            number
                                        }}
                                    }}
                                    fieldValues(first: 10) {{
                                        nodes {{
                                            ... on ProjectV2ItemFieldSingleSelectValue {{
                                                name
                                                field {{
                                                    ... on ProjectV2SingleSelectField {{
                                                        name
                                                    }}
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}'''
            
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True, text=True, check=True, timeout=30
            )
            
            data = json.loads(result.stdout)
            
            # Get project data from the correct path based on owner type
            owner_key = 'user' if owner_type == 'user' else 'organization'
            project_data = data['data'][owner_key]['projectV2']
            
            # Find the item matching our issue number
            for node in project_data['items']['nodes']:
                content = node.get('content')
                if content and content.get('number') == issue_number:
                    # Found the issue, extract status field
                    for field_value in node['fieldValues']['nodes']:
                        if field_value and field_value.get('field', {}).get('name') == 'Status':
                            column_name = field_value.get('name')
                            logger.debug(f"Found issue #{issue_number} in column '{column_name}'")
                            return column_name
            
            logger.debug(f"Issue #{issue_number} not found on board {board_name}")
            return None
            
        except subprocess.CalledProcessError as e:
            logger.error(f"GraphQL query failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Error querying issue column: {e}")
            return None
    
    def _end_run_in_elasticsearch(self, run_data: Dict[str, Any], reason: str, index: Optional[str] = None, outcome: Optional[str] = None):
        """
        End a pipeline run directly in Elasticsearch (cleanup helper)

        Args:
            run_data: Pipeline run data from Elasticsearch
            reason: Reason for ending (for logging)
            index: Optional index name where the document exists (if not provided, uses started_at or today's index)
            outcome: Optional outcome ('success', 'failed', etc.)
        """
        try:
            pipeline_run_id = run_data['id']
            run_data['ended_at'] = datetime.utcnow().isoformat() + 'Z'
            # Mirror end_pipeline_run()'s status derivation: a distinct "failed"
            # status, not "completed" + outcome="failed" simultaneously — the
            # latter is indistinguishable from success by status alone to every
            # status-filtered reader (dashboards, /api/pipeline-runs, etc.).
            run_data['status'] = 'failed' if outcome == 'failed' else 'completed'
            if outcome is not None:
                run_data['outcome'] = outcome

            # Use the provided index (where the document was found) or derive from started_at
            target_index = index
            if not target_index:
                try:
                    started_at = run_data.get('started_at')
                    if started_at:
                        started_at_str = started_at.replace('Z', '+00:00')
                        started_date = datetime.fromisoformat(started_at_str)
                        target_index = self._get_es_index_name(started_date)
                except Exception as e:
                    logger.warning(f"Could not parse started_at '{run_data.get('started_at')}', using current date for index: {e}")
            
            if not target_index:
                target_index = self._get_es_index_name()

            es_index_with_retry(self.es, target_index, run_data, doc_id=pipeline_run_id)

            # Also clean up Redis if it exists
            redis_key = self._get_redis_key(pipeline_run_id)
            if self.redis.exists(redis_key):
                self.redis.setex(
                    redis_key,
                    3600,  # Keep for 1 hour after completion
                    json.dumps(run_data)
                )
            
            # Remove from issue mapping
            project = run_data['project']
            issue_number = run_data['issue_number']
            self._cleanup_issue_mapping(project, issue_number, run_data.get('board'), pipeline_run_id)
            
            logger.info(f"Ended stale pipeline run {pipeline_run_id}: {reason}")
            
        except Exception as e:
            logger.error(f"Error ending pipeline run {run_data.get('id')}: {e}")


# Global pipeline run manager instance
_pipeline_run_manager: Optional[PipelineRunManager] = None


def get_pipeline_run_manager() -> PipelineRunManager:
    """
    Get or create global PipelineRunManager instance
    
    Returns:
        PipelineRunManager instance
    """
    global _pipeline_run_manager
    
    if _pipeline_run_manager is None:
        _pipeline_run_manager = PipelineRunManager()
    
    return _pipeline_run_manager
