#!/usr/bin/env python3
"""
Verify that board columns are correctly configured
"""

import os
import sys
import subprocess
import json

# Add the project root to Python path and change working directory
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

def verify_board_columns():
    """Verify that board columns are correctly configured"""

    print("🔍 Verifying Board Column Configuration")
    print("=" * 50)

    boards = [
        {
            "number": 2,
            "name": "Idea Development Pipeline",
            "expected_columns": ["Backlog", "Research", "Analysis", "Review", "Done"]
        },
        {
            "number": 3,
            "name": "Development Pipeline",
            "expected_columns": ["Backlog", "Requirements", "Design", "Implementation", "Code Review", "Done"]
        },
        {
            "number": 4,
            "name": "Full SDLC Pipeline",
            "expected_columns": ["Backlog", "Research", "Requirements", "Requirements Review", "Design", "Design Review", "Test Planning", "Test Plan Review", "Implementation", "Code Review", "QA Testing", "Documentation", "Documentation Review", "Done"]
        }
    ]

    all_good = True

    for board in boards:
        print(f"\n📋 {board['name']} (Project #{board['number']})")

        try:
            # Get field information
            cmd = ['gh', 'project', 'field-list', str(board['number']), '--owner', 'example_user', '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            fields_data = json.loads(result.stdout)

            # Find Status field
            status_field = None
            for field in fields_data.get('fields', []):
                if field.get('name') == 'Status':
                    status_field = field
                    break

            if not status_field:
                print("❌ No Status field found")
                all_good = False
                continue

            # Get current options
            current_options = [opt.get('name') for opt in status_field.get('options', [])]
            expected_options = board['expected_columns']

            print(f"   Expected: {expected_options}")
            print(f"   Actual:   {current_options}")

            if set(current_options) == set(expected_options):
                print("✅ Columns correctly configured")
            else:
                print("❌ Column mismatch")
                missing = set(expected_options) - set(current_options)
                extra = set(current_options) - set(expected_options)
                if missing:
                    print(f"   Missing: {list(missing)}")
                if extra:
                    print(f"   Extra: {list(extra)}")
                all_good = False

        except Exception as e:
            print(f"❌ Error checking board: {e}")
            all_good = False

    print(f"\n📊 VERIFICATION RESULTS:")
    print("=" * 30)

    if all_good:
        print("🎉 ALL BOARDS CORRECTLY CONFIGURED!")
        print("\n✅ Your multi-board orchestrator system is ready to use:")
        print("   • Create issues with pipeline labels (pipeline:idea-dev, pipeline:dev, pipeline:full-sdlc)")
        print("   • Add stage labels (stage:research, stage:design, etc.)")
        print("   • The webhook system will automatically route work to agents")
        print("   • Agents will move issues through the appropriate status columns")
    else:
        print("❌ Some boards have configuration issues")
        print("   Run: python scripts/fix_existing_boards.py")

    return all_good

if __name__ == "__main__":
    success = verify_board_columns()
    sys.exit(0 if success else 1)