"""The one way a script in scripts/ is allowed to name a state directory.

Five scripts built their own: `Path(os.environ.get('ORCHESTRATOR_ROOT', '.'))`
or `Path(os.environ.get('ORCHESTRATOR_ROOT', Path(__file__).parent.parent))`,
then `/ 'state' / 'projects' / ...`. Both spellings are #181:

  * the `'.'` fallback resolves against the CWD, and the documented way to run
    anything here is from the repository root -- which on the deployment is the
    directory bind-mounted at /app. `python scripts/analyze_codebase.py` from
    the checkout therefore wrote and mkdir'd inside the live state tree.
  * the `__file__` fallback resolves against whatever checkout the script was
    invoked from, which is the same accident with an extra step: run it from a
    worktree and it reads a state tree that is not the one the orchestrator is
    using.

Neither fallback refuses a relative ORCHESTRATOR_ROOT either, so
`ORCHESTRATOR_ROOT=tmp/scratch` -- a dropped leading slash -- silently meant
"under the checkout" for all five.

`orchestrator_state_root()` is the single resolver: env first, the deployment's
own checkout when unset, resolved, and a relative value refused outright.

The import is deferred into the call because `config/state_manager.py` builds a
module-level GitHubStateManager at import time and that constructor mkdirs
`state/projects` and `state/orchestrator`. Importing it at module scope would
make merely importing one of these scripts create directories. The seven
first-party call sites that already existed defer for the same reason
(claude/claude_integration.py, both pipeline/*_checkpoint.py,
services/agent_container_recovery.py, services/observability_server.py,
services/review_cycle.py, state_management/pr_review_state_manager.py);
mcp/server.py becomes the eighth below.
"""

from pathlib import Path


def orchestrator_state_dir() -> Path:
    """The orchestrator's `state/` tree."""
    from config.state_manager import orchestrator_state_root

    return orchestrator_state_root()


def project_state_dir(project: str) -> Path:
    """`state/projects/<project>` in the orchestrator's own state tree."""
    return orchestrator_state_dir() / 'projects' / project
