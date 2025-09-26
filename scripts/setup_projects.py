#!/usr/bin/env python3
"""
Setup script to auto-discover GitHub projects and create projects.yaml
"""

import os
import yaml
from services.project_manager import ProjectManager
from services.github_project_manager import GitHubProjectManager

def main():
    print("🔧 Setting up project configuration...")

    # Initialize managers
    pm = ProjectManager()
    github_pm = GitHubProjectManager()

    # Check if projects.yaml already exists
    if os.path.exists("config/projects.yaml"):
        print("✅ config/projects.yaml already exists")

        # Load and validate existing configuration
        existing_projects = pm.list_configured_projects()
        print(f"📋 Found {len(existing_projects)} configured projects:")
        for project in existing_projects:
            print(f"   - {project}")

        # Ask if user wants to add discovered projects
        response = input("\n🔍 Auto-discover additional GitHub projects? (y/N): ")
        if response.lower() != 'y':
            return

    # Discover GitHub projects
    print("\n🔍 Discovering GitHub repositories...")
    discovered = pm.discover_github_projects()

    if not discovered:
        print("❌ No projects discovered. Make sure GITHUB_TOKEN and GITHUB_ORG are set.")
        print("\nYou can manually create config/projects.yaml using config/projects.example.yaml as a template.")
        return

    # Load existing config or create new
    if os.path.exists("config/projects.yaml"):
        with open("config/projects.yaml", 'r') as f:
            config = yaml.safe_load(f) or {"projects": {}}
    else:
        config = {"projects": {}}

    # Add discovered projects that aren't already configured
    added_count = 0
    for repo_name, ssh_url in discovered.items():
        if repo_name not in config["projects"]:
            config["projects"][repo_name] = {
                "repo_url": ssh_url,
                "tech_stacks": {
                    "frontend": "unknown",  # User can update later
                    "backend": "unknown"
                }
            }
            added_count += 1
            print(f"➕ Added: {repo_name}")

    if added_count == 0:
        print("✅ All discovered projects are already configured")
        return

    # Write updated configuration
    os.makedirs("config", exist_ok=True)
    with open("config/projects.yaml", 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=True)

    print(f"\n✅ Added {added_count} new projects to config/projects.yaml")

    # Configure Kanban boards
    setup_kanban = input("\n📋 Set up Kanban project boards? (y/N): ")
    if setup_kanban.lower() == 'y':
        configure_kanban_boards(config, github_pm)

    print("\n📝 Next steps:")
    print("   1. Edit config/projects.yaml to specify tech_stacks for each project")
    print("   2. Run the orchestrator - projects will be auto-cloned as needed")
    print("   3. Projects will be cloned to: ./projects/{repo-name}/")

def configure_kanban_boards(config: dict, github_pm: GitHubProjectManager):
    """Configure Kanban boards for projects"""
    print("\n📋 Configuring Kanban project boards...")

    # Show available templates
    templates = github_pm.list_templates()
    print(f"\n🎨 Available Kanban templates:")
    for i, template_name in enumerate(templates, 1):
        template_info = github_pm.get_template_info(template_name)
        print(f"   {i}. {template_name}: {template_info.get('description', 'No description')}")

    # Let user choose template
    try:
        choice = input(f"\n🎯 Choose template (1-{len(templates)}) or press Enter for default: ").strip()
        if choice:
            template_name = templates[int(choice) - 1]
        else:
            template_name = None  # Will use default
    except (ValueError, IndexError):
        template_name = None

    print(f"\n🚀 Using template: {template_name or 'default'}")

    updated_projects = 0

    for project_name, project_config in config.get('projects', {}).items():
        if 'kanban_board_id' in project_config:
            print(f"⏭️  {project_name} already has Kanban configuration")
            continue

        print(f"\n📋 Setting up Kanban board for: {project_name}")

        # Check for existing boards
        existing_boards = github_pm.discover_project_boards(project_name)

        if existing_boards:
            print(f"🔍 Found {len(existing_boards)} existing project boards:")
            for i, board in enumerate(existing_boards, 1):
                print(f"   {i}. {board['title']} (#{board['number']})")

            use_existing = input(f"📌 Use existing board? (1-{len(existing_boards)}) or 'n' to create new: ")

            if use_existing.lower() != 'n':
                try:
                    board_index = int(use_existing) - 1
                    selected_board = existing_boards[board_index]

                    # Get columns from existing board
                    columns = github_pm.get_project_columns(selected_board['id'])

                    # Generate configuration
                    kanban_config = {
                        'kanban_board_id': selected_board['number'],
                        'kanban_columns': {col['name']: None for col in columns}  # User can manually assign agents
                    }

                    # Update project config
                    project_config.update(kanban_config)
                    updated_projects += 1

                    print(f"✅ Configured existing board for {project_name}")
                    continue

                except (ValueError, IndexError):
                    print("⚠️ Invalid selection, creating new board...")

        # Create new project board
        board_data = github_pm.create_project_board(project_name, template_name)

        if board_data:
            # Generate kanban configuration
            kanban_config = github_pm.generate_kanban_config(board_data, template_name or github_pm.templates.get('default_template', 'simple'))

            # Update project config
            project_config.update(kanban_config)
            updated_projects += 1

            print(f"✅ Created and configured board for {project_name}")
        else:
            print(f"❌ Failed to create board for {project_name}")

    if updated_projects > 0:
        # Save updated configuration
        with open("config/projects.yaml", 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=True)

        print(f"\n🎉 Updated {updated_projects} projects with Kanban configuration!")
        print("📋 You can now customize agent assignments in config/projects.yaml")
    else:
        print("\n📋 No projects needed Kanban configuration")

if __name__ == "__main__":
    main()