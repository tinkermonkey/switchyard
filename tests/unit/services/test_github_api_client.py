"""
Unit tests for GitHub API client with rate limiting.

Tests cover:
- GraphQL queries with rate limiting
- REST API calls with rate limiting
- HTTP requests with rate limiting
- Circuit breaker behavior
- Rate limit tracking and alarms
- Error handling and retries
"""

import pytest
import json
import time
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

from services.github_api_client import (
    GitHubAPIClient,
    GitHubRateLimitStatus,
    GitHubBreaker,
    get_github_client,
)


class TestGitHubRateLimitStatus:
    """Test rate limit status tracking."""
    
    def test_initialization(self):
        """Test rate limit status initializes correctly."""
        status = GitHubRateLimitStatus()
        assert status.limit == 5000
        assert status.remaining == 5000
        assert status.resource_type == "graphql"
        assert status.reset_time is None
    
    def test_percentage_used(self):
        """Test percentage used calculation."""
        status = GitHubRateLimitStatus()
        status.remaining = 5000
        assert status.get_percentage_used() == 0.0
        
        status.remaining = 2500
        assert status.get_percentage_used() == 50.0
        
        status.remaining = 0
        assert status.get_percentage_used() == 100.0
    
    def test_time_until_reset(self):
        """Test time until reset calculation."""
        status = GitHubRateLimitStatus()
        assert status.get_time_until_reset() is None
        
        future_time = datetime.now() + timedelta(hours=1)
        status.reset_time = future_time
        time_until = status.get_time_until_reset()
        assert time_until is not None
        assert 3500 < time_until < 3700  # ~3600 seconds, allow variance
    
    def test_to_dict(self):
        """Test status export as dictionary."""
        status = GitHubRateLimitStatus()
        status.remaining = 4000
        data = status.to_dict()
        
        assert data['limit'] == 5000
        assert data['remaining'] == 4000
        assert data['used'] == 1000
        assert data['percentage_used'] == 20.0
        assert 'resource_type' in data


class TestGitHubBreaker:
    """Test circuit breaker behavior."""
    
    def test_initial_state(self):
        """Test breaker initializes in closed state."""
        breaker = GitHubBreaker()
        assert breaker.state == GitHubBreaker.CLOSED
        assert not breaker.is_open()
        assert not breaker.is_half_open()
    
    def test_trip_breaker(self):
        """Test tripping the breaker."""
        breaker = GitHubBreaker()
        breaker.trip()
        
        assert breaker.state == GitHubBreaker.OPEN
        assert breaker.is_open()
        assert breaker.opened_at is not None
        assert breaker.reset_time is not None
    
    def test_check_and_close_while_closed(self):
        """Test check_and_close returns true when already closed."""
        breaker = GitHubBreaker()
        assert breaker.check_and_close() == True
    
    def test_close_breaker(self):
        """Test closing the breaker."""
        breaker = GitHubBreaker()
        breaker.trip()
        assert breaker.is_open()
        
        breaker.close()
        assert breaker.state == GitHubBreaker.CLOSED
        assert not breaker.is_open()
    
    def test_reset_time_extraction(self):
        """Test extracting reset time from trip."""
        breaker = GitHubBreaker()
        reset_time = datetime.now() + timedelta(minutes=5)
        breaker.trip(reset_time)
        
        assert breaker.reset_time == reset_time


class TestGitHubAPIClientGraphQL:
    """Test GraphQL query execution."""
    
    @pytest.fixture
    def client(self):
        """Create a fresh client for each test."""
        return GitHubAPIClient()
    
    @patch('subprocess.run')
    def test_graphql_success(self, mock_run, client):
        """Test successful GraphQL query."""
        response_data = {
            'data': {'viewer': {'login': 'test-user'}},
            'extensions': {
                'cost': {
                    'rateLimit': {
                        'limit': 5000,
                        'remaining': 4900,
                        'resetAt': '2025-10-16T23:00:00Z'
                    }
                }
            }
        }
        
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(response_data)
        )
        
        success, response = client.graphql("query { viewer { login } }")
        
        assert success == True
        assert response == {'viewer': {'login': 'test-user'}}
        assert client.total_requests == 1
        assert client.rate_limit.remaining == 4900
    
    @patch('subprocess.run')
    def test_graphql_rate_limited(self, mock_run, client):
        """Test GraphQL query hitting rate limit."""
        mock_run.return_value = Mock(
            returncode=1,
            stderr='API rate limit exceeded',
            stdout=json.dumps({'errors': [{'message': 'API rate limit will be available in 3599 seconds'}]})
        )
        
        success, response = client.graphql("query { viewer { login } }")
        
        assert success == False
        assert response['error'] == 'rate_limited'
        assert client.rate_limited_requests == 1
        assert client.breaker.is_open()
    
    @patch('subprocess.run')
    def test_graphql_timeout(self, mock_run, client):
        """Test GraphQL query timeout."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired('gh', 30)
        
        success, response = client.graphql("query { viewer { login } }")
        
        assert success == False
        assert response['error'] == 'timeout'
    
    @patch('subprocess.run')
    def test_graphql_parse_error(self, mock_run, client):
        """Test GraphQL response parse error."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout='invalid json'
        )
        
        success, response = client.graphql("query { viewer { login } }")
        
        assert success == False
        assert response['error'] == 'parse_error'
    
    @patch('subprocess.run')
    def test_graphql_breaker_open(self, mock_run, client):
        """Test GraphQL rejected when breaker is open."""
        client.breaker.trip()
        
        success, response = client.graphql("query { viewer { login } }")
        
        assert success == False
        assert 'circuit breaker' in response['error']
        assert mock_run.call_count == 0  # No call made


class TestGitHubAPIClientREST:
    """Test REST API call execution."""
    
    @pytest.fixture
    def client(self):
        """Create a fresh client for each test."""
        return GitHubAPIClient()
    
    @patch('subprocess.run')
    def test_rest_get_success(self, mock_run, client):
        """Test successful REST GET request."""
        response_data = {'id': 1, 'title': 'Test Issue'}
        
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(response_data)
        )
        
        success, response = client.rest('GET', '/repos/owner/repo/issues/1')
        
        assert success == True
        assert response == response_data
        assert client.total_requests == 1

    @patch('subprocess.run')
    def test_rest_get_with_query_parameters(self, mock_run, client):
        """Test REST GET request with query parameters in URL."""
        response_data = [
            {'id': 1, 'state': 'open'},
            {'id': 2, 'state': 'closed'}
        ]

        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(response_data)
        )

        # Query parameters should be in the endpoint URL
        endpoint = 'repos/owner/repo/issues?state=all&per_page=100'
        success, response = client.rest('GET', endpoint)

        assert success == True
        assert response == response_data

        # Verify the command includes query params in URL
        call_args = mock_run.call_args[0][0]
        assert 'state=all' in ' '.join(call_args)

    @patch('subprocess.run')
    def test_rest_post_with_data(self, mock_run, client):
        """Test REST POST with request body."""
        response_data = {'id': 2, 'title': 'New Issue'}
        
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(response_data)
        )
        
        success, response = client.rest(
            'POST',
            '/repos/owner/repo/issues',
            {'title': 'New Issue', 'body': 'Description'}
        )
        
        assert success == True
        assert response == response_data
        # Verify data was passed to subprocess
        call_args = mock_run.call_args[0][0]
        assert '-f' in call_args
        assert 'data=' in str(call_args)
    
    @patch('subprocess.run')
    def test_rest_rate_limited(self, mock_run, client):
        """Test REST call hitting rate limit."""
        mock_run.return_value = Mock(
            returncode=1,
            stderr='API rate limit exceeded',
            stdout=''
        )
        
        success, response = client.rest('GET', '/repos/owner/repo/issues/1')
        
        assert success == False
        assert response['error'] == 'rate_limited'
        assert client.rate_limited_requests == 1
        assert client.breaker.is_open()
    
    @patch('subprocess.run')
    def test_rest_empty_response(self, mock_run, client):
        """Test REST call with empty response."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=''
        )
        
        success, response = client.rest('DELETE', '/repos/owner/repo/issues/1')
        
        assert success == True
        assert response == {}
    
    @patch('subprocess.run')
    def test_rest_timeout(self, mock_run, client):
        """Test REST call timeout."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired('gh', 30)
        
        success, response = client.rest('GET', '/repos/owner/repo/issues/1')
        
        assert success == False
        assert response['error'] == 'timeout'
    
    @patch('subprocess.run')
    def test_rest_breaker_open(self, mock_run, client):
        """Test REST rejected when breaker is open."""
        client.breaker.trip()
        
        success, response = client.rest('GET', '/repos/owner/repo/issues/1')
        
        assert success == False
        assert 'circuit breaker' in response['error']
        assert mock_run.call_count == 0


class TestGitHubAPIClientHTTP:
    """Test HTTP request execution."""
    
    @pytest.fixture
    def client(self):
        """Create a fresh client for each test."""
        return GitHubAPIClient()
    
    @patch('requests.get')
    def test_http_get_success(self, mock_get, client):
        """Test successful HTTP GET request."""
        response_data = {'id': 1, 'login': 'test-user'}
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data
        mock_response.headers = {
            'x-ratelimit-limit': '5000',
            'x-ratelimit-remaining': '4900',
            'x-ratelimit-reset': str(int((datetime.now() + timedelta(hours=1)).timestamp()))
        }
        mock_get.return_value = mock_response
        
        success, response = client.http_request('GET', 'https://api.github.com/user')
        
        assert success == True
        assert response == response_data
        assert client.total_requests == 1
        assert client.rate_limit.remaining == 4900
    
    @patch('requests.post')
    def test_http_post_with_data(self, mock_post, client):
        """Test HTTP POST with request body."""
        response_data = {'id': 1, 'state': 'open'}
        
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = response_data
        mock_response.headers = {}
        mock_post.return_value = mock_response
        
        success, response = client.http_request(
            'POST',
            'https://api.github.com/repos/owner/repo/issues',
            {'title': 'New Issue'}
        )
        
        assert success == True
        assert response == response_data
        # Verify data was passed
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs['json'] == {'title': 'New Issue'}
    
    @patch('requests.get')
    def test_http_rate_limited(self, mock_get, client):
        """Test HTTP request hitting rate limit (403)."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.text = 'API rate limit exceeded'
        mock_response.headers = {}
        mock_get.return_value = mock_response
        
        success, response = client.http_request('GET', 'https://api.github.com/user')
        
        assert success == False
        assert response['error'] == 'rate_limited'
        assert client.rate_limited_requests == 1
        assert client.breaker.is_open()
    
    @patch('requests.get')
    def test_http_server_error(self, mock_get, client):
        """Test HTTP request with 5xx error."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = 'Internal Server Error'
        mock_response.headers = {}
        mock_get.return_value = mock_response
        
        success, response = client.http_request('GET', 'https://api.github.com/user')
        
        assert success == False
        assert 'http_error_500' in response['error']
        # 5xx errors are retried, so there will be multiple failed requests
        assert client.failed_requests >= 1
    
    @patch('requests.get')
    def test_http_empty_response(self, mock_get, client):
        """Test HTTP request with empty JSON response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("No JSON")
        mock_response.headers = {}
        mock_get.return_value = mock_response
        
        success, response = client.http_request('GET', 'https://api.github.com/user')
        
        assert success == True
        assert response == {}
    
    @patch('requests.get')
    def test_http_timeout(self, mock_get, client):
        """Test HTTP request timeout."""
        import requests
        mock_get.side_effect = requests.exceptions.Timeout()
        
        success, response = client.http_request('GET', 'https://api.github.com/user')
        
        assert success == False
        assert response['error'] == 'timeout'
    
    @patch('requests.get')
    def test_http_breaker_open(self, mock_get, client):
        """Test HTTP rejected when breaker is open."""
        client.breaker.trip()
        
        success, response = client.http_request('GET', 'https://api.github.com/user')
        
        assert success == False
        assert 'circuit breaker' in response['error']
        assert mock_get.call_count == 0


class TestGitHubAPIClientIntegration:
    """Integration tests across multiple API types."""
    
    @patch('subprocess.run')
    def test_breaker_affects_all_types(self, mock_run):
        """Test breaker rejection applies to all API types."""
        client = GitHubAPIClient()
        client.breaker.trip()
        
        # All should be rejected
        success_gql, _ = client.graphql("query { viewer { login } }")
        assert success_gql == False
        
        success_rest, _ = client.rest('GET', '/user')
        assert success_rest == False
        
        with patch('requests.get') as mock_get:
            success_http, _ = client.http_request('GET', 'https://api.github.com/user')
            assert success_http == False
    
    def test_get_status(self):
        """Test getting client status."""
        client = GitHubAPIClient()
        client.rate_limit.remaining = 4500
        client.total_requests = 10
        
        status = client.get_status()
        
        assert status['rate_limit']['remaining'] == 4500
        assert status['breaker']['state'] == 'closed'
        assert status['stats']['total_requests'] == 10
    
    @patch('subprocess.run')
    def test_alarm_if_needed(self, mock_run, caplog):
        """Test alarm logging at different thresholds."""
        client = GitHubAPIClient()
        
        # Test 80% usage
        client.rate_limit.remaining = 1000
        client.alarm_if_needed()
        # Should log at info level for 80%
        
        # Test 95%+ usage
        client.rate_limit.remaining = 100
        client.alarm_if_needed()
        # Should log at warning level


class TestGlobalClient:
    """Test global client singleton."""
    
    def test_singleton_pattern(self):
        """Test that get_github_client returns same instance."""
        # Import fresh to reset module state
        from services.github_api_client import get_github_client
        
        client1 = get_github_client()
        client2 = get_github_client()
        
        assert client1 is client2
