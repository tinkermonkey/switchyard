"""
Credential identifiers shared across the GitHub auth layer.

services/github_app.py and services/github_api_client.py both have to name the
credential a given call was made on - github_app.py to decide which rate-limit
bucket a response's headers describe (#168), github_api_client.py to do the
same for the calls it routes itself (WI-3). They cannot import the constants
from each other: github_app.py already imports github_api_client at module
level (for is_graphql_rate_limit_error), so the reverse direction would close
an import cycle. This module is deliberately tiny and dependency-free so both
can depend on it instead, which they do.
"""

CREDENTIAL_APP = 'app'
CREDENTIAL_PAT = 'pat'
