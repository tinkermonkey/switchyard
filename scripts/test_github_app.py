#!/usr/bin/env python3
"""
Test script for GitHub App authentication and Discussions API

Run this to verify:
1. GitHub App can authenticate
2. Installation token can be generated
3. Discussions API is accessible
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from services.github_app import github_app
from services.github_discussions import GitHubDiscussions


def test_github_app_auth():
    """Test GitHub App authentication"""
    print("=" * 60)
    print("Testing GitHub App Authentication")
    print("=" * 60)

    if not github_app.enabled:
        print("❌ GitHub App is not enabled")
        print("   Check that GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID,")
        print("   and GITHUB_APP_PRIVATE_KEY_PATH are set correctly")
        return False

    print("✓ GitHub App initialized")
    print(f"  App ID: {github_app.app_id}")
    print(f"  Installation ID: {github_app.installation_id}")

    # Try to get installation token
    print("\nGenerating installation token...")
    token = github_app.get_installation_token()

    if token:
        print("✓ Installation token generated successfully")
        print(f"  Token: {token[:20]}...{token[-10:]}")
        print(f"  Expires: {github_app._token_expires_at}")
        return True
    else:
        print("❌ Failed to generate installation token")
        return False


def test_discussions_api(owner: str, repo: str):
    """Test Discussions API access"""
    print("\n" + "=" * 60)
    print(f"Testing Discussions API for {owner}/{repo}")
    print("=" * 60)

    discussions = GitHubDiscussions()

    # Test 1: Get discussion categories
    print("\n1. Getting discussion categories...")
    categories = discussions.get_discussion_categories(owner, repo)

    if categories:
        print(f"✓ Found {len(categories)} categories:")
        for cat in categories:
            emoji = cat.get('emoji', '📋')
            name = cat['name']
            print(f"  {emoji} {name} (ID: {cat['id'][:20]}...)")
    else:
        print("❌ Failed to get categories")
        return False

    # Test 2: List recent discussions
    print("\n2. Listing recent discussions...")
    recent_discussions = discussions.list_discussions(owner, repo, first=5)

    if recent_discussions:
        print(f"✓ Found {len(recent_discussions)} recent discussions:")
        for disc in recent_discussions[:3]:
            print(f"  #{disc['number']}: {disc['title']}")
            print(f"    Category: {disc['category']['name']}")
            print(f"    Updated: {disc['updatedAt']}")
    else:
        print("⚠️  No discussions found (or repo doesn't have discussions enabled)")

    # Test 3: Search for mentions
    print("\n3. Searching for @orchestrator-bot mentions...")
    mentioned = discussions.search_discussions_for_mentions(owner, repo)

    if mentioned:
        print(f"✓ Found {len(mentioned)} discussion(s) with @orchestrator-bot mentions:")
        for disc in mentioned[:3]:
            print(f"  #{disc['number']}: {disc['title']}")
    else:
        print("⚠️  No mentions found")

    return True


def main():
    """Run all tests"""
    print("\n🔧 GitHub App & Discussions API Test Suite\n")

    # Test authentication
    auth_success = test_github_app_auth()

    if not auth_success:
        print("\n❌ Authentication failed - cannot continue with API tests")
        sys.exit(1)

    # Get repo from environment or use default
    owner = os.environ.get('GITHUB_ORG', 'tinkermonkey')
    repo = os.environ.get('GITHUB_REPO', 'context-studio')

    # Test Discussions API
    api_success = test_discussions_api(owner, repo)

    print("\n" + "=" * 60)
    if auth_success and api_success:
        print("✅ All tests passed!")
        print("\nYou can now:")
        print("  - Create discussions programmatically")
        print("  - Post comments as orchestrator-bot[bot]")
        print("  - Monitor discussions for @mentions")
    else:
        print("⚠️  Some tests had issues")
    print("=" * 60)


if __name__ == "__main__":
    main()
