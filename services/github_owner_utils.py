"""
GitHub Owner Type Detection Utility

This module provides utilities to determine whether a GitHub owner (login)
is a User or Organization, which is required for correctly querying
GitHub Projects v2 API.
"""

import subprocess
import json
import logging
from typing import Literal, Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

OwnerType = Literal['user', 'organization']


@lru_cache(maxsize=128)
def get_owner_type(owner_login: str) -> Optional[OwnerType]:
    """
    Determine if a GitHub owner is a User or Organization.
    
    Args:
        owner_login: GitHub username or organization name
        
    Returns:
        'user' or 'organization', or None if unable to determine
    """
    try:
        # Query GitHub API to get owner type
        result = subprocess.run(
            ['gh', 'api', f'/users/{owner_login}', '--jq', '.type'],
            capture_output=True,
            text=True,
            timeout=5,
            check=True
        )
        
        owner_type = result.stdout.strip().lower()
        
        if owner_type == 'user':
            logger.debug(f"Owner '{owner_login}' is a User")
            return 'user'
        elif owner_type == 'organization':
            logger.debug(f"Owner '{owner_login}' is an Organization")
            return 'organization'
        else:
            logger.warning(f"Unknown owner type '{owner_type}' for '{owner_login}'")
            return None
            
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to determine owner type for '{owner_login}': {e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Error determining owner type for '{owner_login}': {e}")
        return None


def build_projects_v2_query(owner_login: str, project_number: int) -> Optional[str]:
    """
    Build a GraphQL query for GitHub Projects v2 based on owner type.
    
    Args:
        owner_login: GitHub username or organization name
        project_number: Project number
        
    Returns:
        GraphQL query string, or None if owner type cannot be determined
    """
    owner_type = get_owner_type(owner_login)
    
    if owner_type is None:
        logger.error(f"Cannot build Projects v2 query - unable to determine owner type for '{owner_login}'")
        return None
    
    # Determine the correct GraphQL query based on owner type
    if owner_type == 'user':
        query = f'''{{
            user(login: "{owner_login}") {{
                projectV2(number: {project_number}) {{
                    id
                    title
                    items(first: 100) {{
                        nodes {{
                            id
                            content {{
                                __typename
                                ... on Issue {{
                                    id
                                    number
                                    title
                                    state
                                    repository {{
                                        name
                                    }}
                                    updatedAt
                                }}
                            }}
                            fieldValues(first: 10) {{
                                nodes {{
                                    ... on ProjectV2ItemFieldSingleSelectValue {{
                                        name
                                        field {{
                                            ... on ProjectV2SingleSelectField {{
                                                name
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}'''
    else:  # organization
        query = f'''{{
            organization(login: "{owner_login}") {{
                projectV2(number: {project_number}) {{
                    id
                    title
                    items(first: 100) {{
                        nodes {{
                            id
                            content {{
                                __typename
                                ... on Issue {{
                                    id
                                    number
                                    title
                                    state
                                    repository {{
                                        name
                                    }}
                                    updatedAt
                                }}
                            }}
                            fieldValues(first: 10) {{
                                nodes {{
                                    ... on ProjectV2ItemFieldSingleSelectValue {{
                                        name
                                        field {{
                                            ... on ProjectV2SingleSelectField {{
                                                name
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}'''
    
    return query


def get_projects_list_for_owner(owner_login: str) -> Optional[list]:
    """
    Get list of projects for a GitHub owner (user or organization).
    
    Args:
        owner_login: GitHub username or organization name
        
    Returns:
        List of projects, or None if unable to fetch
    """
    owner_type = get_owner_type(owner_login)
    
    if owner_type is None:
        logger.error(f"Cannot list projects - unable to determine owner type for '{owner_login}'")
        return None
    
    try:
        # For users, use GraphQL to list projects
        if owner_type == 'user':
            query = f'''{{
                user(login: "{owner_login}") {{
                    projectsV2(first: 100) {{
                        nodes {{
                            id
                            number
                            title
                            url
                        }}
                    }}
                }}
            }}'''
            
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True,
                text=True,
                timeout=10,
                check=True
            )
            
            data = json.loads(result.stdout)
            return data.get('data', {}).get('user', {}).get('projectsV2', {}).get('nodes', [])
            
        else:  # organization
            query = f'''{{
                organization(login: "{owner_login}") {{
                    projectsV2(first: 100) {{
                        nodes {{
                            id
                            number
                            title
                            url
                        }}
                    }}
                }}
            }}'''
            
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True,
                text=True,
                timeout=10,
                check=True
            )
            
            data = json.loads(result.stdout)
            return data.get('data', {}).get('organization', {}).get('projectsV2', {}).get('nodes', [])
            
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to list projects for '{owner_login}': {e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Error listing projects for '{owner_login}': {e}")
        return None
