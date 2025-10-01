"""
GitHub App Authentication Module

Handles JWT-based authentication for GitHub Apps, providing installation tokens
for API access with proper bot identification.
"""

import os
import time
import jwt
import requests
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

class GitHubAppAuth:
    """Manages GitHub App authentication and token generation"""

    def __init__(self):
        """Initialize GitHub App authentication"""
        self.app_id = os.environ.get('GITHUB_APP_ID')
        self.installation_id = os.environ.get('GITHUB_APP_INSTALLATION_ID')

        # Private key can be provided as path or directly as string
        self.private_key_path = os.environ.get('GITHUB_APP_PRIVATE_KEY_PATH')
        self.private_key_content = os.environ.get('GITHUB_APP_PRIVATE_KEY')

        self.private_key = None
        self.installation_token = None
        self.token_expires_at = None

        # Load private key
        if self.private_key_path:
            self._load_private_key_from_file()
        elif self.private_key_content:
            self.private_key = self.private_key_content

        # Validate configuration
        if self.is_configured():
            logger.debug("GitHub App authentication configured")
            logger.debug(f"App ID: {self.app_id}")
            logger.debug(f"Installation ID: {self.installation_id}")
        else:
            logger.warning("GitHub App authentication not configured, will use PAT fallback")

    def _load_private_key_from_file(self):
        """Load private key from file"""
        try:
            key_path = Path(self.private_key_path).expanduser()

            if not key_path.exists():
                logger.error(f"Private key file not found: {key_path}")
                return

            with open(key_path, 'r') as f:
                self.private_key = f.read()

            logger.debug(f"Loaded private key from {key_path}")

        except Exception as e:
            logger.error(f"Failed to load private key: {e}")

    def is_configured(self) -> bool:
        """Check if GitHub App is properly configured"""
        return bool(self.app_id and self.installation_id and self.private_key)

    def generate_jwt(self) -> str:
        """
        Generate a JWT for GitHub App authentication
        Valid for 10 minutes
        """
        if not self.private_key:
            raise ValueError("Private key not available")

        # JWT claims
        now = int(time.time())
        payload = {
            'iat': now,  # Issued at
            'exp': now + (10 * 60),  # Expires in 10 minutes
            'iss': self.app_id  # Issuer (App ID)
        }

        # Generate JWT
        token = jwt.encode(payload, self.private_key, algorithm='RS256')

        return token

    def get_installation_token(self, force_refresh: bool = False) -> Optional[str]:
        """
        Get an installation access token for the GitHub App

        Tokens are cached and automatically refreshed when expired.
        Valid for 1 hour.

        Args:
            force_refresh: Force token refresh even if not expired

        Returns:
            Installation token or None if failed
        """
        if not self.is_configured():
            return None

        # Check if we have a valid cached token
        if not force_refresh and self.installation_token and self.token_expires_at:
            from datetime import timezone
            if datetime.now(timezone.utc) < self.token_expires_at:
                return self.installation_token

        try:
            # Generate JWT for authentication
            jwt_token = self.generate_jwt()

            # Request installation token
            url = f"https://api.github.com/app/installations/{self.installation_id}/access_tokens"

            headers = {
                'Authorization': f'Bearer {jwt_token}',
                'Accept': 'application/vnd.github.v3+json'
            }

            response = requests.post(url, headers=headers)
            response.raise_for_status()

            data = response.json()

            # Cache the token
            self.installation_token = data['token']

            # Parse expiration (GitHub provides expires_at in ISO format)
            expires_at_str = data.get('expires_at')
            if expires_at_str:
                # Parse ISO format and subtract 5 minutes for safety margin
                self.token_expires_at = datetime.fromisoformat(
                    expires_at_str.replace('Z', '+00:00')
                ) - timedelta(minutes=5)
            else:
                # Default to 55 minutes from now if not provided
                self.token_expires_at = datetime.now() + timedelta(minutes=55)

            logger.info(f"Generated new installation token, expires at {self.token_expires_at}")

            return self.installation_token

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get installation token: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return None
        except Exception as e:  
            logger.error(f"Error generating installation token: {e}")
            return None

    def get_app_info(self) -> Optional[Dict[str, Any]]:
        """Get information about the GitHub App"""
        if not self.is_configured():
            return None

        try:
            jwt_token = self.generate_jwt()

            headers = {
                'Authorization': f'Bearer {jwt_token}',
                'Accept': 'application/vnd.github.v3+json'
            }

            response = requests.get('https://api.github.com/app', headers=headers)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Failed to get app info: {e}")
            return None

    def get_installation_info(self) -> Optional[Dict[str, Any]]:
        """Get information about the app installation"""
        token = self.get_installation_token()
        if not token:
            return None

        try:
            headers = {
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github.v3+json'
            }

            url = f"https://api.github.com/app/installations/{self.installation_id}"
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Failed to get installation info: {e}")
            return None


# Global instance
_github_app_auth: Optional[GitHubAppAuth] = None

def get_github_app_auth() -> GitHubAppAuth:
    """Get or create the global GitHub App auth instance"""
    global _github_app_auth

    if _github_app_auth is None:
        _github_app_auth = GitHubAppAuth()

    return _github_app_auth