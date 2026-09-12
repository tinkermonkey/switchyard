#!/usr/bin/env python3
"""
Manually set dev container status to VERIFIED for a project.
Usage: python3 scripts/set_dev_container_verified.py <project_name>
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.dev_container_state import dev_container_state, DevContainerStatus
from services.dev_container_build_lock import dev_container_build_lock_sync, DevContainerBuildLockTimeoutError

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/set_dev_container_verified.py <project_name>")
        sys.exit(1)

    project_name = sys.argv[1]

    # (#198) The tag belongs to the dev-container ENVIRONMENT, which may be
    # shared with other projects. Marking it verified marks it verified for
    # every member, so say so rather than letting an operator believe this
    # override is scoped to one project.
    from services.dev_container_environment import environment_for, image_tag_for

    image_name = image_tag_for(project_name)
    environment = environment_for(project_name)
    if environment != project_name:
        print(
            f"Note: {project_name} shares dev-container environment "
            f"'{environment}'. This override applies to every project in it."
        )

    # dev_container_build lock (#56): this is one of the two admin scripts
    # that used to bypass PipelineLockManager entirely and could race a live
    # pipeline-driven build/verify for the same project. Acquiring the same
    # project-level lock the pipeline path uses (claude/claude_integration.py)
    # before touching dev_container_state serializes this operator override
    # against any in-flight dev_environment_setup/verifier run.
    try:
        with dev_container_build_lock_sync(project_name):
            # Set status to VERIFIED
            dev_container_state.set_status(
                project_name=project_name,
                status=DevContainerStatus.VERIFIED,
                image_name=image_name
            )

            print(f"✓ Marked {project_name} dev container as VERIFIED")
            print(f"  Image: {image_name}")

            # Verify it was set
            status = dev_container_state.get_status(project_name)
            print(f"  Current status: {status.value}")
    except DevContainerBuildLockTimeoutError as e:
        print(f"✗ {e}")
        sys.exit(1)
