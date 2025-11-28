"""
Integration tests for GitHubAPIClient - the centralized GitHub API client.

These tests validate the ACTUAL GitHubAPIClient.graphql() method used by:
- services/github_project_manager.py - Project mutations
- services/github_integration.py - GraphQL query wrapper
- services/project_monitor.py - Project item polling
- services/pipeline_queue_manager.py - Issue queries

Run with: pytest tests/integration/test_github_api_client_integration.py -v -s

Prerequisites:
- GITHUB_TOKEN set in .env file (automatically loaded by tests)
- Tests default to: org=tinkermonkey, repo=codetoreum, project=26, issue=76
"""

import pytest
import os
import sys
from pathlib import Path

# Load .env file if it exists (for GITHUB_TOKEN authentication)
env_file = Path(__file__).parent.parent.parent / '.env'
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                # Only set if not already in environment
                if key not in os.environ:
                    os.environ[key] = value

# Import GitHubAPIClient after .env is loaded
from services.github_api_client import GitHubAPIClient, get_github_client

# Test configuration
TEST_ORG = os.getenv('GITHUB_TEST_ORG', 'tinkermonkey')
TEST_REPO = os.getenv('GITHUB_TEST_REPO', 'codetoreum')
TEST_PROJECT_NUMBER = int(os.getenv('GITHUB_TEST_PROJECT_NUMBER', '26'))
TEST_ISSUE_NUMBER = int(os.getenv('GITHUB_TEST_ISSUE', '76'))


@pytest.fixture(scope='session')
def test_config():
    """Display test configuration."""
    print(f"\nGitHubAPIClient Integration Test Configuration:")
    print(f"  Organization: {TEST_ORG}")
    print(f"  Repository: {TEST_REPO}")
    print(f"  Project Number: {TEST_PROJECT_NUMBER}")
    print(f"  Issue Number: {TEST_ISSUE_NUMBER}")
    print(f"  GITHUB_TOKEN: {'✓ Set' if os.getenv('GITHUB_TOKEN') else '✗ Not set'}")
    
    if not os.getenv('GITHUB_TOKEN'):
        pytest.skip("GITHUB_TOKEN not found in environment or .env file")


@pytest.fixture(scope='session')
def github_client():
    """Get GitHubAPIClient instance."""
    return get_github_client()


class TestGitHubAPIClientBasics:
    """Test basic GitHubAPIClient functionality."""

    def test_client_initialization(self, github_client):
        """Test that GitHubAPIClient initializes correctly."""
        assert github_client is not None
        assert hasattr(github_client, 'graphql')
        assert hasattr(github_client, 'rate_limit')
        assert hasattr(github_client, 'breaker')

    def test_singleton_pattern(self):
        """Test that get_github_client() returns the same instance."""
        client1 = get_github_client()
        client2 = get_github_client()
        assert client1 is client2, "Should return same instance (singleton)"


class TestProjectMonitorQueries:
    """
    Test GraphQL queries used by project_monitor.py.
    
    This validates the critical polling queries that detect card movements
    and trigger agent execution.
    """

    def test_query_project_items(self, github_client, test_config):
        """
        Test querying project items with issue details.
        
        This is the main query used by project_monitor.py to poll
        for card movements between columns.
        """
        # Try organization first
        org_query = '''
        query($owner: String!, $projectNumber: Int!, $cursor: String) {
            organization(login: $owner) {
                projectV2(number: $projectNumber) {
                    items(first: 100, after: $cursor) {
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                        nodes {
                            id
                            content {
                                ... on Issue {
                                    number
                                    title
                                    state
                                    url
                                }
                            }
                            fieldValues(first: 20) {
                                nodes {
                                    ... on ProjectV2ItemFieldSingleSelectValue {
                                        name
                                        field {
                                            ... on ProjectV2SingleSelectField {
                                                name
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        '''

        org_variables = {
            'owner': TEST_ORG,
            'projectNumber': TEST_PROJECT_NUMBER,
            'cursor': None
        }

        success, result = github_client.graphql(org_query, org_variables)

        if not success or not result.get('organization'):
            # Try user query
            user_query = org_query.replace('organization', 'user')
            success, result = github_client.graphql(user_query, org_variables)

        assert success, f"Query failed: {result}"
        assert 'data' in result or 'user' in result or 'organization' in result

        # Validate structure
        project_data = result.get('user', result.get('organization', {})).get('projectV2')
        if project_data:
            assert 'items' in project_data
            assert 'nodes' in project_data['items']
            items = project_data['items']['nodes']
            assert isinstance(items, list)

            # Validate item structure if any items exist
            if items:
                first_item = items[0]
                assert 'id' in first_item
                # content might be None for draft items
                assert 'fieldValues' in first_item


class TestGitHubIntegrationQueries:
    """
    Test GraphQL queries used by github_integration.py.
    
    Validates the graphql_query() wrapper method.
    """

    def test_issue_query_with_variables(self, github_client, test_config):
        """
        Test querying issue details with variables.
        
        This validates the pattern used by github_integration.graphql_query()
        which passes variables to the GraphQL API.
        """
        query = '''
        query($owner: String!, $repo: String!, $issueNumber: Int!) {
            repository(owner: $owner, name: $repo) {
                issue(number: $issueNumber) {
                    id
                    number
                    title
                    state
                    body
                    author {
                        login
                    }
                    createdAt
                    updatedAt
                }
            }
        }
        '''

        variables = {
            'owner': TEST_ORG,
            'repo': TEST_REPO,
            'issueNumber': TEST_ISSUE_NUMBER
        }

        success, result = github_client.graphql(query, variables)

        assert success, f"Query with variables failed: {result}"
        assert 'repository' in result
        assert 'issue' in result['repository']
        
        issue = result['repository']['issue']
        assert issue['number'] == TEST_ISSUE_NUMBER
        assert 'title' in issue
        assert 'state' in issue


class TestGitHubProjectManagerQueries:
    """
    Test GraphQL queries used by github_project_manager.py.
    
    Validates project structure queries and mutations (read-only tests).
    """

    def test_query_project_fields(self, github_client, test_config):
        """
        Test querying project fields and options.
        
        This is used before mutations to verify field structure.
        """
        # Try organization first
        org_query = '''
        query($owner: String!, $projectNumber: Int!) {
            organization(login: $owner) {
                projectV2(number: $projectNumber) {
                    id
                    title
                    fields(first: 20) {
                        nodes {
                            ... on ProjectV2Field {
                                id
                                name
                            }
                            ... on ProjectV2SingleSelectField {
                                id
                                name
                                options {
                                    id
                                    name
                                }
                            }
                        }
                    }
                }
            }
        }
        '''

        org_variables = {
            'owner': TEST_ORG,
            'projectNumber': TEST_PROJECT_NUMBER
        }

        success, result = github_client.graphql(org_query, org_variables)

        if not success or not result.get('organization'):
            # Try user query
            user_query = org_query.replace('organization', 'user')
            success, result = github_client.graphql(user_query, org_variables)

        assert success, f"Query failed: {result}"
        
        project_data = result.get('user', result.get('organization', {})).get('projectV2')
        assert project_data, "Project not found"
        assert 'fields' in project_data
        assert 'nodes' in project_data['fields']
        
        fields = project_data['fields']['nodes']
        assert isinstance(fields, list)
        assert len(fields) > 0, "Project should have at least some fields"


class TestCircuitBreakerIntegration:
    """Test that circuit breaker is integrated with GitHubAPIClient."""

    def test_circuit_breaker_exists(self, github_client):
        """Test that GitHubAPIClient has circuit breaker."""
        assert hasattr(github_client, 'breaker')
        assert github_client.breaker is not None

        # Verify circuit breaker has required attributes
        assert hasattr(github_client.breaker, 'state')
        assert hasattr(github_client.breaker, 'check_and_close')

    def test_rate_limit_tracking(self, github_client):
        """Test that rate limit tracking is initialized."""
        assert hasattr(github_client, 'rate_limit')
        assert github_client.rate_limit is not None
        
        # Verify rate limit has required attributes
        assert hasattr(github_client.rate_limit, 'limit')
        assert hasattr(github_client.rate_limit, 'remaining')
        assert hasattr(github_client.rate_limit, 'get_percentage_used')


class TestErrorHandling:
    """Test error handling in GitHubAPIClient."""

    def test_invalid_query_returns_false(self, github_client):
        """Test that invalid queries return (False, error_data)."""
        invalid_query = "{ invalid syntax }"
        
        success, result = github_client.graphql(invalid_query)
        
        # Should return False for invalid query
        assert success is False
        # Should return error information
        assert result is not None

    def test_nonexistent_field_query(self, github_client, test_config):
        """Test querying a field that doesn't exist."""
        query = '''
        query {
            viewer {
                nonExistentField
            }
        }
        '''
        
        success, result = github_client.graphql(query)
        
        # GraphQL should return an error
        assert success is False or 'errors' in result


class TestRealWorldUsagePatterns:
    """Test actual usage patterns from the orchestrator."""

    def test_project_monitor_pattern(self, github_client, test_config):
        """
        Test the exact pattern used in project_monitor.py.
        
        This validates: github_client.graphql(query) with no variables.
        """
        # Simplified version of the project monitor query
        query = f'''
        {{
            repository(owner: "{TEST_ORG}", name: "{TEST_REPO}") {{
                issue(number: {TEST_ISSUE_NUMBER}) {{
                    number
                    title
                }}
            }}
        }}
        '''
        
        # project_monitor calls without variables parameter
        success, result = github_client.graphql(query)
        
        assert success, f"Project monitor pattern failed: {result}"
        assert 'repository' in result
        assert 'issue' in result['repository']

    def test_github_integration_pattern(self, github_client, test_config):
        """
        Test the exact pattern used in github_integration.py.
        
        This validates: github_client.graphql(query, variables)
        """
        query = '''
        query($owner: String!, $repo: String!) {
            repository(owner: $owner, name: $repo) {
                name
                owner {
                    login
                }
            }
        }
        '''
        
        variables = {
            'owner': TEST_ORG,
            'repo': TEST_REPO
        }
        
        # github_integration calls with variables
        success, result = github_client.graphql(query, variables)
        
        assert success, f"GitHub integration pattern failed: {result}"
        assert 'repository' in result
        assert result['repository']['name'] == TEST_REPO


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
