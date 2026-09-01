"""
Baked-dependency extraction for per-epic worktrees (issue #50).

## The bug this works around

The runtime bind mount that puts a project's checkout into an agent container
(see claude/docker_runner.py's `mount_spec`, `-v {host_project_path}:/workspace:...`)
lands at container path `/workspace` (the root of the mount), not
`/workspace/{PROJECT_NAME}`. A Docker bind mount replaces the ENTIRE subtree at its
mount point the instant the container starts. So anything a project's
Dockerfile.agent bakes at build time under `/workspace/*` -- e.g. `node_modules`
from a build-time `npm install`, or a Python venv -- is completely shadowed and
unreachable at runtime, no matter how deep under `/workspace` it was baked, and
regardless of any `WORKDIR` the Dockerfile set (agents are launched with a fixed
`-w /workspace`, overriding it).

## The fix (two halves)

1. (prompts/content/agents/dev_environment_setup/guidelines.md) New/regenerated
   Dockerfile.agents bake dependencies OUT of the `/workspace` subtree entirely, at
   BAKED_DEPS_PATH below, mirroring each dependency directory's normal in-tree
   relative location underneath it (e.g. `{BAKED_DEPS_PATH}/node_modules`,
   `{BAKED_DEPS_PATH}/.venv`). Nothing under `/workspace` means nothing a bind mount
   does can ever shadow it.
2. (this module) Once a worktree exists, `extract_baked_dependencies()` pulls that
   out-of-tree directory back out of the built image and copies it into the
   worktree, landing each dependency directory at the normal in-tree relative path
   an agent would expect to find it (e.g. `<worktree>/node_modules`).

## Self-healing rollout

A project whose image predates this convention (still baked at the old, shadowed
`/workspace/*` location) simply has nothing at BAKED_DEPS_PATH to extract. That's
expected, not an error: extraction no-ops with a clear log line flagging the
project's image as due for a Dockerfile.agent regeneration, and the caller
(services/project_workspace.py's get_or_create_epic_worktree) proceeds with worktree
creation regardless. This module never raises and never attempts to install
dependencies itself -- that stays the dev-environment agent's responsibility.
"""

import logging
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Out-of-tree path new Dockerfile.agents bake dependencies at -- see this module's
# docstring and guidelines.md's "Stage 3: Pre-install Dependencies". Deliberately
# outside /workspace entirely so the runtime bind mount can never shadow it.
BAKED_DEPS_PATH = "/opt/deps"

_DOCKER_TIMEOUT_SECONDS = 60  # create/rm: cheap, near-instant operations
# Copying a large baked node_modules/.venv can genuinely take minutes -- using the
# same short timeout as create/rm was found (#50 review) to silently and
# permanently defeat this feature for exactly the large-dependency projects it's
# meant to help.
_DOCKER_CP_TIMEOUT_SECONDS = 300


def extract_baked_dependencies(project_name: str, image_name: str, destination: Path) -> bool:
    """Best-effort copy of an image's out-of-tree baked dependencies into a worktree.

    Runs:
        docker create --name tmp-extract-<project>-<random> <image_name>
        docker cp tmp-extract-<project>-<random>:/opt/deps/. <destination>
        docker rm -f tmp-extract-<project>-<random>

    A single `docker cp` doubles as both the existence check and the copy: Docker
    validates the source path exists server-side before it starts streaming any data,
    so a missing BAKED_DEPS_PATH (old-convention image) fails immediately without
    transferring anything -- there's no cost to skipping a separate existence probe.

    Never raises. Any failure (Docker unavailable, image missing, BAKED_DEPS_PATH
    absent because the image predates this convention, a genuine copy error) is
    logged and this returns False so the caller can proceed with worktree creation
    regardless -- a dependency-extraction problem must never block worktree creation.

    Args:
        project_name: Project name, used only to name the scratch container.
        image_name: The project's built agent image (e.g. "my-project-agent:latest").
        destination: Worktree root to copy the baked dependencies into. Must already
            exist (it does -- this is only ever called after `git worktree add`
            succeeds).

    Returns:
        True if BAKED_DEPS_PATH was found in the image and copied into destination.
        False if extraction was skipped or failed for any reason.
    """
    container_name = f"tmp-extract-{project_name}-{uuid.uuid4().hex[:8]}"
    # Three states: None (unknown -- 'docker create's own subprocess.run() hasn't
    # returned yet, e.g. it's mid-flight or raised, so the daemon may or may not
    # have actually created the container), True (confirmed created), False
    # (confirmed NOT created -- docker create explicitly reported failure). Cleanup
    # in `finally` below is skipped ONLY for the confirmed-False case (a wasted-but-
    # harmless `docker rm -f` on a container we know was never created); it still
    # runs for None, since an exception during `docker create` (e.g. TimeoutExpired)
    # doesn't tell us whether the daemon created the container before the client
    # gave up -- skipping cleanup there would risk a permanent leak.
    created = None
    try:
        create_result = subprocess.run(
            ['docker', 'create', '--name', container_name, image_name],
            capture_output=True, text=True, timeout=_DOCKER_TIMEOUT_SECONDS
        )
        if create_result.returncode != 0:
            created = False
            logger.warning(
                f"Baked-dependency extraction for {project_name}: 'docker create' from "
                f"image {image_name!r} failed ({create_result.stderr.strip()}); skipping "
                "-- worktree creation proceeds without pre-baked dependencies."
            )
            return False
        created = True

        copy_result = subprocess.run(
            ['docker', 'cp', f'{container_name}:{BAKED_DEPS_PATH}/.', str(destination)],
            capture_output=True, text=True, timeout=_DOCKER_CP_TIMEOUT_SECONDS
        )
        if copy_result.returncode != 0:
            stderr = copy_result.stderr.strip()
            # Best-effort classification of "old-convention image, nothing at
            # BAKED_DEPS_PATH" (expected/self-healing) vs. a genuine copy failure.
            # Brittle by nature (depends on Docker CLI wording, which can shift
            # across versions/locales) -- a misclassification only affects which
            # log message is shown, never control flow (both branches return False
            # and proceed identically), so the risk is a confusing log line, not a
            # behavior change.
            stderr_lower = stderr.lower()
            looks_like_missing_path = any(
                phrase in stderr_lower
                for phrase in ('no such', 'not found', 'does not exist')
            )
            if looks_like_missing_path:
                logger.warning(
                    f"Project {project_name}'s image ({image_name}) has nothing at "
                    f"{BAKED_DEPS_PATH} -- it predates the out-of-tree baked-dependency "
                    "convention (issue #50) and is due for a Dockerfile.agent "
                    "regeneration. Continuing without pre-baked dependencies in the new "
                    "worktree; this is expected and self-healing, not a fatal error."
                )
            else:
                logger.warning(
                    f"Baked-dependency extraction for {project_name}: 'docker cp' of "
                    f"{BAKED_DEPS_PATH} into {destination} failed ({stderr}); worktree "
                    "creation proceeds. Note: docker cp streams incrementally, so a "
                    "partial/incomplete dependency tree may already be present at "
                    "destination -- a subsequent fresh install by the dev-environment "
                    "agent is expected to reconcile this, not something this module "
                    "attempts to roll back itself."
                )
            return False

        logger.info(
            f"Extracted baked dependencies from {image_name} ({BAKED_DEPS_PATH}) into "
            f"{destination} for {project_name}'s new worktree"
        )
        return True
    except Exception as e:
        logger.warning(
            f"Baked-dependency extraction for {project_name} raised unexpectedly ({e}); "
            "skipping -- worktree creation must not be blocked by this."
        )
        return False
    finally:
        # Unconditional, not gated on `created`: `docker rm -f` on a container that
        # was never actually created is a harmless no-op (nonzero exit, silently
        # ignored below). Gating on `created` left a real leak when `docker create`'s
        # own subprocess.run() raised (e.g. TimeoutExpired at the timeout boundary)
        # AFTER the daemon had already created the container server-side -- `created`
        # would never be set to True in that case, so cleanup was skipped for a
        # container that genuinely existed, leaking it permanently (its randomized
        # name means nothing else can ever rediscover and remove it).
        if created is not False:  # True (confirmed) or None (unknown) -- attempt cleanup
            try:
                rm_result = subprocess.run(
                    ['docker', 'rm', '-f', container_name],
                    capture_output=True, text=True, timeout=_DOCKER_TIMEOUT_SECONDS
                )
                if created and rm_result.returncode != 0:
                    logger.warning(
                        f"Failed to remove scratch container {container_name}: "
                        f"{rm_result.stderr.strip()}"
                    )
            except Exception as e:
                logger.warning(f"Failed to remove scratch container {container_name}: {e}")
