#!/usr/bin/env python3
"""
Test script for GitHub App authentication

This script verifies that the GitHub App is properly configured and can authenticate.
"""

import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.github_app_auth import get_github_app_auth
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

def test_github_app_auth():
    """Test GitHub App authentication"""

    print("\n" + "="*60)
    print("GitHub App Authentication Test")
    print("="*60 + "\n")

    # Get auth instance
    auth = get_github_app_auth()

    # Check configuration
    print("1. Configuration Check:")
    print("-" * 40)

    if not auth.is_configured():
        print("   Status: NOT CONFIGURED")
        print("\n   GitHub App authentication is not configured.")
        print("   The orchestrator will fall back to using GITHUB_TOKEN (PAT).")
        print("\n   To configure GitHub App:")
        print("   1. Follow the guide at documentation/github_app_setup.md")
        print("   2. Set these environment variables:")
        print("      - GITHUB_APP_ID")
        print("      - GITHUB_APP_INSTALLATION_ID")
        print("      - GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY")
        return False

    print(f"   Status: CONFIGURED")
    print(f"   App ID: {auth.app_id}")
    print(f"   Installation ID: {auth.installation_id}")
    print(f"   Private Key: {'Loaded from file' if auth.private_key_path else 'Loaded from env'}")

    # Test JWT generation
    print("\n2. JWT Generation Test:")
    print("-" * 40)

    try:
        jwt_token = auth.generate_jwt()
        print(f"   Status: SUCCESS")
        print(f"   JWT Token: {jwt_token[:50]}...")
    except Exception as e:
        print(f"   Status: FAILED")
        print(f"   Error: {e}")
        return False

    # Test installation token
    print("\n3. Installation Token Test:")
    print("-" * 40)

    try:
        token = auth.get_installation_token()

        if token:
            print(f"   Status: SUCCESS")
            print(f"   Token: {token[:20]}...")
            print(f"   Expires at: {auth.token_expires_at}")
        else:
            print(f"   Status: FAILED")
            print(f"   Could not get installation token")
            return False

    except Exception as e:
        print(f"   Status: FAILED")
        print(f"   Error: {e}")
        return False

    # Test app info retrieval
    print("\n4. GitHub App Info Test:")
    print("-" * 40)

    try:
        app_info = auth.get_app_info()

        if app_info:
            print(f"   Status: SUCCESS")
            print(f"   App Name: {app_info.get('name')}")
            print(f"   App Owner: {app_info.get('owner', {}).get('login')}")
            print(f"   Description: {app_info.get('description', 'N/A')}")
        else:
            print(f"   Status: FAILED")
            print(f"   Could not retrieve app info")
            return False

    except Exception as e:
        print(f"   Status: FAILED")
        print(f"   Error: {e}")
        return False

    # Test installation info retrieval
    print("\n5. Installation Info Test:")
    print("-" * 40)

    try:
        install_info = auth.get_installation_info()

        if install_info:
            print(f"   Status: SUCCESS")
            print(f"   Account: {install_info.get('account', {}).get('login')}")
            print(f"   Target Type: {install_info.get('target_type')}")
            print(f"   Repository Selection: {install_info.get('repository_selection')}")
        else:
            print(f"   Status: FAILED")
            print(f"   Could not retrieve installation info")
            return False

    except Exception as e:
        print(f"   Status: FAILED")
        print(f"   Error: {e}")
        return False

    print("\n" + "="*60)
    print("All tests passed! GitHub App is properly configured.")
    print("="*60 + "\n")

    print("Next steps:")
    print("1. Rebuild containers: docker-compose up -d --build")
    print("2. Comments from orchestrator will now appear as 'orchestrator-bot[bot]'")
    print("3. The 'isBot' flag will be properly set to true")

    return True

if __name__ == "__main__":
    try:
        success = test_github_app_auth()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)