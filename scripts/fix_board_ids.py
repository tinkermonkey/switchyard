#!/usr/bin/env python3
"""
Fix board field IDs by querying GitHub for actual option IDs

This script updates existing board state that has placeholder IDs
with the actual GitHub GraphQL option IDs.
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.state_manager import GitHubStateManager
from config.manager import ConfigManager

def main():
    state_manager = GitHubStateManager()
    config_manager = ConfigManager()

    # Get all projects
    projects = config_manager.list_projects()

    for project_name in projects:
        print(f"\nProcessing project: {project_name}")

        state = state_manager.load_project_state(project_name)
        if not state:
            print(f"  No state found for {project_name}")
            continue

        # Refresh each board
        for board_name in state.boards.keys():
            print(f"  Refreshing board: {board_name}")
            success = state_manager.refresh_board_field_ids(project_name, board_name)
            if success:
                print(f"    ✓ Successfully updated {board_name}")
            else:
                print(f"    ✗ Failed to update {board_name}")

    print("\nDone!")

if __name__ == "__main__":
    main()
