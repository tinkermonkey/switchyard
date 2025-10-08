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

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/set_dev_container_verified.py <project_name>")
        sys.exit(1)

    project_name = sys.argv[1]
    image_name = f"{project_name}-agent:latest"

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
