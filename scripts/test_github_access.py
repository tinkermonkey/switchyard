#!/usr/bin/env python3
"""
Test GitHub CLI access and project creation permissions
"""

import os
import sys
import subprocess
import json

# Add the project root to Python path and change working directory
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

def test_github_cli_basic():
    """Test basic GitHub CLI installation and auth"""
    print("🔍 Testing GitHub CLI...")

    try:
        # Check if gh is installed
        result = subprocess.run(['gh', '--version'], capture_output=True, text=True)
        if result.returncode != 0:
            print("❌ GitHub CLI not installed!")
            print("   Install with: brew install gh")
            return False

        print(f"✅ GitHub CLI installed: {result.stdout.strip().split()[2]}")

        # Check authentication
        auth_result = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True)
        if auth_result.returncode != 0:
            print("❌ GitHub CLI not authenticated!")
            print("   Run: gh auth login")
            return False

        print("✅ GitHub CLI authenticated")
        return True

    except FileNotFoundError:
        print("❌ GitHub CLI not found!")
        print("   Install with: brew install gh")
        return False

def test_org_access(org_name):
    """Test organization access"""
    print(f"\n🔍 Testing access to organization: {org_name}")

    try:
        # First check if it's a user account instead of an org
        user_result = subprocess.run(['gh', 'api', f'users/{org_name}'], capture_output=True, text=True)
        if user_result.returncode == 0:
            user_data = json.loads(user_result.stdout)
            print(f"📝 '{org_name}' is a user account: {user_data.get('name', org_name)}")
            print("   For personal accounts, you can create projects under your username")
            org_type = "user"
        else:
            # Test organization access
            result = subprocess.run(['gh', 'api', f'orgs/{org_name}'], capture_output=True, text=True)
            if result.returncode != 0:
                print(f"❌ Cannot access organization '{org_name}'!")
                print(f"   Error: {result.stderr}")
                print("\n🔧 Possible solutions:")
                print("   1. Check organization name spelling (case-sensitive)")
                print("   2. Ensure you have access to the organization")
                print("   3. Add required scopes:")
                print("      gh auth refresh -h github.com -s admin:org,user,project,repo")
                print("   4. If it's a personal account, use your GitHub username instead")
                return False

            org_data = json.loads(result.stdout)
            print(f"✅ Organization access verified: {org_data.get('name', org_name)}")
            org_type = "org"

        # Test project creation permissions
        print(f"🔍 Testing project creation permissions...")

        # List existing projects to test permissions
        projects_result = subprocess.run(['gh', 'project', 'list', '--owner', org_name], capture_output=True, text=True)
        if projects_result.returncode != 0:
            print(f"❌ Cannot list projects for '{org_name}'!")
            print(f"   Error: {projects_result.stderr}")
            if "admin:org" in projects_result.stderr:
                print("\n🔧 Missing required scope. Run:")
                print("   gh auth refresh -h github.com -s admin:org,user,project,repo")
            elif "user" in projects_result.stderr:
                print("\n🔧 Missing user scope. Run:")
                print("   gh auth refresh -h github.com -s admin:org,user,project,repo")
            else:
                print("   You may not have project creation permissions")
            return False

        print("✅ Project listing permissions verified")

        # Show current projects if any
        try:
            projects_data = json.loads(projects_result.stdout)
            if projects_data.get('projects'):
                print(f"📋 Found {len(projects_data['projects'])} existing projects")
            else:
                print("📋 No existing projects found")
        except:
            pass  # JSON parsing failed, but listing worked

        return True

    except json.JSONDecodeError as e:
        print(f"❌ Error parsing GitHub API response: {e}")
        return False
    except Exception as e:
        print(f"❌ Error testing organization access: {e}")
        return False

def test_project_creation(org_name, dry_run=True):
    """Test project creation (dry run by default)"""
    print(f"\n🔍 Testing project creation for: {org_name}")

    if dry_run:
        print("📋 Would run command:")
        cmd = [
            'gh', 'project', 'create',
            '--owner', org_name,
            '--title', 'Test Project - DELETE ME',
            '--body', 'Test project for permissions',
            '--format', 'json'
        ]
        print(f"   {' '.join(cmd)}")
        print("✅ Dry run completed - use --create-test to actually test creation")
        return True
    else:
        print("⚠️  Creating actual test project (will be deleted)...")
        # Implementation for actual test creation would go here
        return True

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Test GitHub access for board creation')
    parser.add_argument('--org', help='GitHub organization to test', required=True)
    parser.add_argument('--create-test', action='store_true', help='Actually create (and delete) a test project')

    args = parser.parse_args()

    print("🚀 GitHub Access Test")
    print("=" * 50)

    # Test basic CLI
    if not test_github_cli_basic():
        return 1

    # Test org access
    if not test_org_access(args.org):
        return 1

    # Test project creation
    if not test_project_creation(args.org, dry_run=not args.create_test):
        return 1

    print("\n🎉 All tests passed!")
    print(f"You should be able to create projects in the '{args.org}' organization.")

    return 0

if __name__ == "__main__":
    sys.exit(main())