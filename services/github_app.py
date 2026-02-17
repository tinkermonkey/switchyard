"""
GitHub App Integration for Orchestrator Bot

Handles authentication and API calls using GitHub App installation tokens.
This allows the bot to comment as "orchestrator-bot[bot]" instead of impersonating users.
"""

import os
import time
import jwt
import requests
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class GitHubApp:
    """GitHub App authentication and API client"""

    def __init__(self):
        """Initialize GitHub App with credentials from environment"""
        self.app_id = os.environ.get('GITHUB_APP_ID')
        self.installation_id = os.environ.get('GITHUB_APP_INSTALLATION_ID')
        self.private_key_path = os.environ.get('GITHUB_APP_PRIVATE_KEY_PATH')
        self._installation_token = None
        self._token_expires_at = None

        if not all([self.app_id, self.installation_id, self.private_key_path]):
            logger.warning("GitHub App credentials not fully configured - some features may be limited")
            self.enabled = False
            return

        # Load private key
        try:
            with open(self.private_key_path, 'r') as key_file:
                self.private_key = key_file.read()
            self.enabled = True
            logger.info(f"GitHub App initialized (App ID: {self.app_id}, Installation ID: {self.installation_id})")
        except Exception as e:
            logger.error(f"Failed to load GitHub App private key: {e}")
            self.enabled = False

    def _generate_jwt(self) -> str:
        """Generate JWT for GitHub App authentication"""
        now = int(time.time())
        payload = {
            'iat': now,
            'exp': now + (10 * 60),  # Expires in 10 minutes
            'iss': self.app_id
        }

        return jwt.encode(payload, self.private_key, algorithm='RS256')

    def get_installation_token(self) -> Optional[str]:
        """Get or refresh installation access token"""
        if not self.enabled:
            return None

        # Check if token is still valid
        if self._installation_token and self._token_expires_at:
            from datetime import timezone
            now = datetime.now(timezone.utc)
            if now < self._token_expires_at - timedelta(minutes=5):
                return self._installation_token

        # Generate new token
        try:
            jwt_token = self._generate_jwt()
            headers = {
                'Authorization': f'Bearer {jwt_token}',
                'Accept': 'application/vnd.github.v3+json'
            }

            url = f'https://api.github.com/app/installations/{self.installation_id}/access_tokens'
            response = requests.post(url, headers=headers)
            response.raise_for_status()

            data = response.json()
            self._installation_token = data['token']
            self._token_expires_at = datetime.fromisoformat(data['expires_at'].replace('Z', '+00:00'))

            logger.info(f"Generated new GitHub App installation token (expires: {self._token_expires_at})")
            return self._installation_token

        except Exception as e:
            logger.error(f"Failed to get installation token: {e}")
            return None

    def _invalidate_token(self):
        """Invalidate cached installation token so the next call generates a fresh one."""
        self._installation_token = None
        self._token_expires_at = None

    def _get_token(self, force_refresh: bool = False) -> Optional[str]:
        """Get an authentication token, preferring App installation token with PAT fallback."""
        if self.enabled:
            if force_refresh:
                self._invalidate_token()
            token = self.get_installation_token()
            if token:
                return token
            if force_refresh:
                logger.warning("App token refresh failed, falling back to PAT")

        return os.environ.get('GITHUB_TOKEN')

    def graphql_request(self, query: str, variables: Dict[str, Any] = None) -> Optional[Dict]:
        """Execute a GraphQL request using GitHub App authentication (with PAT fallback)"""

        token = self._get_token()
        if not token:
            logger.error("No installation token or PAT available for GraphQL request")
            return None

        payload = {'query': query}
        if variables:
            payload['variables'] = variables

        try:
            response = requests.post(
                'https://api.github.com/graphql',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Accept': 'application/vnd.github.v3+json'
                },
                json=payload
            )

            # On 401, refresh the token and retry once.
            # GitHub rejects 401 before processing, so retrying mutations is safe.
            if response.status_code == 401 and self.enabled:
                logger.warning("GraphQL request got 401, refreshing token and retrying")
                token = self._get_token(force_refresh=True)
                if token:
                    response = requests.post(
                        'https://api.github.com/graphql',
                        headers={
                            'Authorization': f'Bearer {token}',
                            'Accept': 'application/vnd.github.v3+json'
                        },
                        json=payload
                    )

                    # If retry also fails with 401, fall back to PAT
                    if response.status_code == 401:
                        pat = os.environ.get('GITHUB_TOKEN')
                        if pat and pat != token:
                            logger.warning("GraphQL retry still 401, falling back to PAT")
                            response = requests.post(
                                'https://api.github.com/graphql',
                                headers={
                                    'Authorization': f'Bearer {pat}',
                                    'Accept': 'application/vnd.github.v3+json'
                                },
                                json=payload
                            )
                        elif not pat:
                            logger.error("GraphQL retry still 401 and no GITHUB_TOKEN configured for PAT fallback")
                else:
                    logger.error("Cannot retry GraphQL request, no token available after refresh")

            response.raise_for_status()

            data = response.json()
            if 'errors' in data:
                # Check if all errors are NOT_FOUND (common for deleted resources)
                errors = data['errors']
                all_not_found = all(err.get('type') == 'NOT_FOUND' for err in errors)

                if all_not_found:
                    logger.debug(f"GraphQL NOT_FOUND errors: {errors}")
                else:
                    logger.error(f"GraphQL errors: {errors}")
                return None

            return data.get('data')

        except requests.exceptions.HTTPError as e:
            logger.error(f"GraphQL request failed: {e}")
            return None
        except Exception as e:
            logger.error(f"GraphQL request failed (unexpected): {e}", exc_info=True)
            return None

    def _rest_call(self, method: str, url: str, headers: Dict, data: Dict = None) -> requests.Response:
        """Execute a single REST API call."""
        method = method.upper()
        if method == 'GET':
            return requests.get(url, headers=headers)
        elif method == 'POST':
            return requests.post(url, headers=headers, json=data)
        elif method == 'PATCH':
            return requests.patch(url, headers=headers, json=data)
        elif method == 'DELETE':
            return requests.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

    def rest_request(self, method: str, path: str, data: Dict = None) -> Optional[Dict]:
        """Execute a REST API request using GitHub App authentication (with PAT fallback)"""

        token = self._get_token()
        if not token:
            logger.error("No installation token or PAT available for REST request")
            return None

        url = f'https://api.github.com{path}'
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        try:
            response = self._rest_call(method, url, headers, data)

            # On 401, refresh the token and retry once.
            # GitHub rejects 401 before processing, so retrying mutations is safe.
            if response.status_code == 401 and self.enabled:
                logger.warning(f"REST request got 401 for {method} {path}, refreshing token and retrying")
                token = self._get_token(force_refresh=True)
                if token:
                    headers['Authorization'] = f'Bearer {token}'
                    response = self._rest_call(method, url, headers, data)

                    # If retry also fails with 401, fall back to PAT
                    if response.status_code == 401:
                        pat = os.environ.get('GITHUB_TOKEN')
                        if pat and pat != token:
                            logger.warning(f"REST retry still 401 for {method} {path}, falling back to PAT")
                            headers['Authorization'] = f'Bearer {pat}'
                            response = self._rest_call(method, url, headers, data)
                        elif not pat:
                            logger.error(f"REST retry still 401 for {method} {path} and no GITHUB_TOKEN configured for PAT fallback")
                else:
                    logger.error(f"Cannot retry REST request for {method} {path}, no token available after refresh")

            response.raise_for_status()
            return response.json() if response.text else {}

        except requests.exceptions.HTTPError as e:
            logger.error(f"REST request failed: {e}")
            return None
        except Exception as e:
            logger.error(f"REST request failed (unexpected): {e}", exc_info=True)
            return None


# Global singleton instance
github_app = GitHubApp()
