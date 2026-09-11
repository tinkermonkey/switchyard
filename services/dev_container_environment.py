"""
Dev-container environments (#198).

A dev container is a *named environment*, not a per-project artifact. Several
project configs that point at the same repository can name the same
environment and share one image, one state file and one build lock between
them:

    # features.yaml, bugs.yaml, infra.yaml -- the same block in each
    dev_container:
      environment: "monorepo"

Absent, an environment resolves to the project's own name, so a deployment
that configures none behaves byte-identically to before this existed: every
key is the project name, exactly as it was.

## There is no owner

An earlier draft had one project own the image and the others borrow it. That
was wrong: it made project B undispatchable until project A's *board pipeline*
had run, with B unable to see, trigger or influence the thing blocking it, and
deleting A's config silently broke B.

Membership here is symmetric and the build is first-come. Whichever member
reaches its Environment Support board first acquires the build lock (keyed on
the environment, so members serialize against each other) and builds; every
member arriving afterwards re-reads the state, finds it VERIFIED, and no-ops.
Any member can produce the resource, so removing every other config leaves the
survivor able to build unaided. Nothing is privileged and nothing needs
designating.

## Why the lookup is cached

ConfigManager.get_project_config() re-reads from disk on every call, by
design ("always reading from disk to pick up runtime edits"). This resolver
sits underneath DevContainerStateManager.get_state_file(), i.e. underneath
every dev-container state read, so an uncached lookup would turn each of those
into a YAML parse. A short TTL keeps the runtime-edit property in spirit -- a
changed environment name takes effect within CACHE_TTL_SECONDS rather than
instantly -- without putting a file read under every status check.
"""

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tag component the image name is built from: `{environment}-agent:latest`.
IMAGE_TAG_SUFFIX = "-agent:latest"

# See "Why the lookup is cached" above.
CACHE_TTL_SECONDS = 10.0

# Docker rejects a tag component that isn't [a-zA-Z0-9][a-zA-Z0-9_.-]*, and the
# environment name becomes one verbatim. Validated at config load so a bad name
# fails there rather than inside an agent's `docker build` an hour later.
_VALID_ENVIRONMENT_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')

_cache: Dict[str, Tuple[float, str]] = {}


def clear_cache() -> None:
    """Drop memoized lookups. For tests, and for any caller that has just
    rewritten a project config and needs the change to take effect now."""
    _cache.clear()


def _lookup_environment(project_name: str) -> str:
    """Uncached read of `project_name`'s configured environment, or its own name.

    Never raises: a project with no config at all (system-level callers pass
    names that aren't real projects) resolves to itself, which is exactly the
    pre-#198 behaviour.
    """
    try:
        from config.manager import config_manager
        project_config = config_manager.get_project_config(project_name)
    except Exception as e:
        logger.debug(
            f"No project config for {project_name!r} while resolving its dev-container "
            f"environment ({e}); using the project name"
        )
        return project_name

    dev_container = getattr(project_config, 'dev_container', None) or {}
    environment = dev_container.get('environment')
    if not environment:
        return project_name

    if not isinstance(environment, str) or not _VALID_ENVIRONMENT_NAME.match(environment):
        # Config validation rejects this at load, so reaching here means a
        # config was edited underneath a running orchestrator. Degrade to the
        # project's own name rather than emitting an unusable docker tag.
        logger.error(
            f"Project {project_name!r} declares an invalid dev-container environment "
            f"{environment!r}; falling back to the project name"
        )
        return project_name

    return environment


def environment_for(project_name: str) -> str:
    """The dev-container environment `project_name` participates in.

    Returns `project_name` itself when none is configured, so this is an
    identity function for every deployment that hasn't opted in.
    """
    now = time.monotonic()
    cached = _cache.get(project_name)
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    environment = _lookup_environment(project_name)
    _cache[project_name] = (now, environment)
    return environment


def image_tag_for(project_name: str) -> str:
    """The Docker tag `project_name`'s agents run from, e.g. `monorepo-agent:latest`.

    The single source of truth for that string. It is handed to the
    dev_environment_setup agent verbatim (see that agent's guidelines) rather
    than composed from the project name at the point of use -- the tag and the
    checkout path stop being the same identifier once environments exist, and
    asking a model to keep them apart on every run is the kind of instruction
    that fails quietly.
    """
    return f"{environment_for(project_name)}{IMAGE_TAG_SUFFIX}"


def members_of(environment: str, project_names: List[str]) -> List[str]:
    """Which of `project_names` participate in `environment`."""
    return sorted(p for p in project_names if environment_for(p) == environment)


def validate_environments(
    project_configs: Dict[str, 'object'],
) -> List[str]:
    """Config-load validation for dev-container environments (#198).

    `project_configs` maps project name -> ProjectConfig. Returns a list of
    human-readable errors, empty when valid; the caller decides whether to
    raise, matching ConfigManager's existing error-collecting validators.

    Three rules, each catching a misconfiguration that is otherwise only
    visible as confusing runtime behaviour:

    1. Members of one environment must agree on `github.repo_url`. Sharing an
       image across different codebases is never correct, and this is the one
       structural check that can detect it -- the image bakes one repo's
       dependencies, so a second repo would silently run against the wrong
       ones.
    2. The name must be a legal Docker tag component, because it becomes one.
    3. An environment must not be named after a *different* project, which
       would quietly make that project's implicit environment someone else's
       explicit one and couple two unrelated projects' images together.
    """
    errors: List[str] = []

    def _env_of(cfg) -> Optional[str]:
        dev_container = getattr(cfg, 'dev_container', None) or {}
        return dev_container.get('environment')

    # Rule 2 -- name legality
    for name, cfg in sorted(project_configs.items()):
        environment = _env_of(cfg)
        if environment is None:
            continue
        if not isinstance(environment, str) or not _VALID_ENVIRONMENT_NAME.match(environment):
            errors.append(
                f"Project '{name}' declares dev_container.environment "
                f"{environment!r}, which is not a valid Docker tag component "
                f"(it becomes '{environment}{IMAGE_TAG_SUFFIX}')"
            )

    # Rule 3 -- collision with a different project's implicit environment
    for name, cfg in sorted(project_configs.items()):
        environment = _env_of(cfg)
        if environment is None or environment == name:
            continue
        if environment in project_configs and _env_of(project_configs[environment]) != environment:
            errors.append(
                f"Project '{name}' declares dev_container.environment "
                f"'{environment}', which is also the name of a different project "
                f"that has not opted into that environment. Either have "
                f"'{environment}' declare the same environment explicitly, or "
                f"pick a name that is not a project name."
            )

    # Rule 1 -- members must share a repository
    by_environment: Dict[str, List[str]] = {}
    for name, cfg in sorted(project_configs.items()):
        environment = _env_of(cfg) or name
        by_environment.setdefault(environment, []).append(name)

    for environment, members in sorted(by_environment.items()):
        if len(members) < 2:
            continue
        repos = {}
        for member in members:
            github = getattr(project_configs[member], 'github', None) or {}
            repos[member] = github.get('repo_url')
        distinct = {r for r in repos.values() if r}
        if len(distinct) > 1:
            detail = ', '.join(f"{m}={repos[m]!r}" for m in members)
            errors.append(
                f"Projects sharing dev_container.environment '{environment}' have "
                f"different github.repo_url values ({detail}). A dev-container image "
                f"bakes one repository's dependencies, so sharing it across "
                f"repositories would run agents against the wrong environment."
            )

    return errors
