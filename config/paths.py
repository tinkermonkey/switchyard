"""The roots every path in this repository hangs off, resolved in one place.

WHY THIS MODULE EXISTS RATHER THAN LIVING IN config/state_manager.py, where
`orchestrator_state_root()` was first written (#202): importing
config.state_manager runs `state_manager = GitHubStateManager()` at module
scope, which builds a full ConfigManager and MKDIRS the state tree. Measured in
the container on this branch: `import config.state_manager` took 0.041s and
created `state/orchestrator/` and `state/projects/` under the checkout it was
imported from. That is an acceptable cost for a process entrypoint -- for
main.py and services/observability_server.py the side effect IS the
fail-at-start -- and an unacceptable one for `scripts/validate_artifacts.py`,
which only wants to know a directory name and must not create a state tree as a
condition of being asked. So the resolution lives here, with no imports beyond
os and pathlib and no side effects at all, and config/state_manager.py
re-exports `orchestrator_state_root` so that every existing
`from config.state_manager import orchestrator_state_root` keeps working.

THE BUG THESE FUNCTIONS EXIST TO REFUSE. Callers open-coded

    root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
    state_dir = Path(root) / "state" / "<subdir>"
    state_dir.mkdir(parents=True, exist_ok=True)   # a line or two later

with no strip, no resolve and no validation. `os.environ.get(key, '/app')`
returns `''` when the key EXISTS BUT IS EMPTY -- which `-e ORCHESTRATOR_ROOT=`
on a docker run is -- so the `/app` default never applied to that case, and
`Path('') / "state"` is the CWD-relative `state`, mkdir'd wherever the process
happened to be standing. config/state_manager.py and
state_management/pr_review_state_manager.py had a second shape of the same
fault: they derived from `Path(__file__).parent.parent`, i.e. from whatever
checkout the code was imported from (#181). The documented way to run the unit
suite is `pytest tests/unit` from the repository root, and on the deployment
the repository root IS the directory bind-mounted at /app -- so the suite wrote
its fixtures into the live state tree and the production watchdog did real work
on them.

RESOLVED, AND RELATIVE VALUES REFUSED, because the obvious guard does not work.
`/app` and `/workspace/switchyard` are the same inode on the deployment --
docker-compose mounts the checkout twice -- so a test asserting `root != '/app'`
passes for `/workspace/switchyard`, for `.`, and for `/app/../app`, every one of
which writes production. A dropped leading slash in `-e ORCHESTRATOR_ROOT=/tmp/...`,
the likeliest typo in the documented command, is the same class of accident: a
relative root means "somewhere under the current directory", and the current
directory is the checkout.

Refusing is the right severity. There is no sensible reading of a relative
root, the value is set once at container start, and failing at the first call is
how an operator finds out in a second rather than after a sweep has run
somewhere unintended.
"""

import os
from pathlib import Path
from typing import Optional

# The checkout this file was imported from. config/ and scripts/ are siblings,
# so this is the same directory every caller's own
# `Path(__file__).parent.parent` used to compute -- which is what makes
# substituting this in a no-op on the deployment, where ORCHESTRATOR_ROOT is
# unset (verified: `'ORCHESTRATOR_ROOT' in os.environ` is False in
# switchyard-orchestrator-1) and /app IS the checkout.
_CHECKOUT_ROOT = Path(__file__).parent.parent.resolve()

_DEFAULT_WORKSPACE_ROOT = Path('/workspace')


def root_from_env(name: str) -> Optional[Path]:
    """The absolute, resolved value of root env var `name`, or None if unset.

    Unset, empty and whitespace-only all read as None -- deliberately the same
    answer, because `-e FOO=` and a trailing space in a .env line are typos,
    not requests to use the current working directory. Anything else that is
    not absolute raises: see the module docstring for why refusing beats
    warning here.
    """
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return None

    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError(
            f"{name} must be an absolute path, got {raw!r}. A relative value "
            f"resolves against the current working directory, which for the "
            f"documented invocation is the checkout itself -- so it would "
            f"write state into the deployment (#181)."
        )
    return root.resolve()


def orchestrator_root() -> Path:
    """The orchestrator checkout this process owns, as an absolute path.

    ORCHESTRATOR_ROOT first; unset falls back to the checkout this code was
    imported from, which on the deployment is /app.

    Callers that want a STATE path want orchestrator_state_root() instead --
    this returns the root itself, for the two kinds of caller that need it:
    services/data_retention.resolve_roots(), which hangs rules off the root
    rather than off root/state, and the scripts/ entry points, which derive
    both `root/state/projects/...` and `root.parent` (the workspace) from it.
    """
    return root_from_env('ORCHESTRATOR_ROOT') or _CHECKOUT_ROOT


def orchestrator_state_root() -> Path:
    """The `state/` tree this process owns, as a resolved absolute path.

    This is the only resolver for state paths in config/, services/, pipeline/,
    state_management/ and scripts/. Nothing else in those trees reads
    ORCHESTRATOR_ROOT to build one; tests/unit/test_state_root_all_doors.py
    holds that true for the modules that used to, and
    tests/unit/test_state_root_isolation.py holds it true for the repository as
    a whole.

    HOW CALLERS IMPORT IT. The library modules under services/ do it INSIDE the
    branch that needs it, not at module scope, because they import it via
    config.state_manager, whose import builds the GitHubStateManager singleton
    (see this module's own docstring). services/observability_server.py
    deliberately does the opposite, and says why on the import itself: for a
    process entrypoint that side effect IS the fail-at-start, which is what
    main.py:16 already gets. Anything importing from config.paths directly can
    do so at module scope -- that is the point of this module having no side
    effects.
    """
    return orchestrator_root() / "state"


def workspace_root() -> Path:
    """Where the managed project checkouts live, as an absolute path.

    WORKSPACE_ROOT first, then /workspace. NOT derived from
    orchestrator_root(): in the container /app IS /workspace/switchyard, so the
    two are the same inode by two names and neither can be computed from the
    other. services/data_retention.py is the caller that needs both, because
    some of what it sweeps lives beside the checkout (`/workspace/.orchestrator`,
    `/workspace/<project>/`) rather than inside it.
    """
    return root_from_env('WORKSPACE_ROOT') or _DEFAULT_WORKSPACE_ROOT
