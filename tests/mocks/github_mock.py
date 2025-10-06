"""
Mock GitHub API for testing

Provides in-memory simulation of GitHub GraphQL and REST APIs
to enable fast, deterministic testing without real API calls.
"""

import json
from typing import Dict, List, Optional, Any
from datetime import datetime


class MockGitHubApp:
    """Mock GitHub App that simulates GraphQL and REST responses"""

    def __init__(self):
        self._discussions = {}
        self._comments = {}
        self._projects = {}
        self._installation_token = "mock_token_12345"
        self._call_log = []  # Track all API calls for assertions

    async def graphql_request(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
        """
        Simulate GraphQL request responses

        Detects query type and returns appropriate mock data
        """
        self._call_log.append({
            'type': 'graphql',
            'query': query,
            'variables': variables,
            'timestamp': datetime.now().isoformat()
        })

        variables = variables or {}

        # Discussion query
        if 'discussion(' in query or 'node(id:' in query:
            discussion_id = variables.get('discussionId')
            if discussion_id and discussion_id in self._discussions:
                return {'node': self._discussions[discussion_id]}
            return None

        # Add discussion comment mutation
        if 'addDiscussionComment' in query:
            discussion_id = variables.get('discussionId')
            body = variables.get('body')
            reply_to_id = variables.get('replyToId')

            comment_id = f"mock_comment_{len(self._comments) + 1}"
            comment = {
                'id': comment_id,
                'body': body,
                'author': {'login': 'orchestrator-bot'},
                'createdAt': datetime.now().isoformat(),
                'replies': {'nodes': []}
            }

            self._comments[comment_id] = comment

            # Add to discussion
            if discussion_id in self._discussions:
                if reply_to_id:
                    # Add as reply
                    for comment_node in self._discussions[discussion_id]['comments']['nodes']:
                        if comment_node['id'] == reply_to_id:
                            comment_node['replies']['nodes'].append(comment)
                            break
                else:
                    # Add as top-level comment
                    self._discussions[discussion_id]['comments']['nodes'].append(comment)

            return {
                'addDiscussionComment': {
                    'comment': comment
                }
            }

        # Project queries
        if 'project(' in query or 'projectV2' in query:
            project_id = variables.get('projectId')
            if project_id and project_id in self._projects:
                return {'node': self._projects[project_id]}
            return None

        return None

    def rest_request(self, method: str, path: str, data: Optional[Dict] = None) -> Optional[Dict]:
        """Simulate REST API request"""
        self._call_log.append({
            'type': 'rest',
            'method': method,
            'path': path,
            'data': data,
            'timestamp': datetime.now().isoformat()
        })

        # Simulate successful responses
        if method == 'POST' and '/issues/' in path and '/comments' in path:
            return {
                'id': f"mock_issue_comment_{len(self._comments) + 1}",
                'body': data.get('body', ''),
                'user': {'login': 'orchestrator-bot'},
                'created_at': datetime.now().isoformat()
            }

        return None

    def get_installation_token(self) -> str:
        """Return mock installation token"""
        return self._installation_token

    # Test utility methods

    def load_discussion_fixture(self, discussion_id: str, fixture_data: Dict):
        """
        Pre-load discussion data for tests

        Args:
            discussion_id: Discussion ID (e.g., 'D_kwDOPH6wk84AiPtN')
            fixture_data: Discussion data structure matching GitHub GraphQL response
        """
        self._discussions[discussion_id] = fixture_data

    def load_discussion_from_file(self, discussion_id: str, fixture_path: str):
        """Load discussion fixture from JSON file"""
        with open(fixture_path) as f:
            data = json.load(f)
            # Extract the discussion node if wrapped in repository structure
            if 'repository' in data and 'discussion' in data['repository']:
                discussion_data = data['repository']['discussion']
            elif 'node' in data:
                discussion_data = data['node']
            else:
                discussion_data = data

            self._discussions[discussion_id] = discussion_data

    def add_comment_to_discussion(
        self,
        discussion_id: str,
        body: str,
        author: str = 'orchestrator-bot',
        reply_to_id: Optional[str] = None
    ) -> str:
        """
        Programmatically add a comment to a mock discussion

        Returns: Comment ID
        """
        comment_id = f"mock_comment_{len(self._comments) + 1}"
        comment = {
            'id': comment_id,
            'body': body,
            'author': {'login': author},
            'createdAt': datetime.now().isoformat(),
            'replies': {'nodes': []}
        }

        self._comments[comment_id] = comment

        if discussion_id in self._discussions:
            if reply_to_id:
                # Add as reply
                for comment_node in self._discussions[discussion_id]['comments']['nodes']:
                    if comment_node['id'] == reply_to_id:
                        comment_node['replies']['nodes'].append(comment)
                        break
            else:
                # Add as top-level comment
                self._discussions[discussion_id]['comments']['nodes'].append(comment)

        return comment_id

    def create_discussion(
        self,
        discussion_id: str,
        title: str,
        body: str,
        category: str = 'Ideas'
    ) -> str:
        """
        Create a new mock discussion

        Returns: Discussion ID
        """
        discussion = {
            'id': discussion_id,
            'title': title,
            'body': body,
            'category': {'name': category},
            'author': {'login': 'orchestrator-bot'},
            'createdAt': datetime.now().isoformat(),
            'comments': {'nodes': []}
        }

        self._discussions[discussion_id] = discussion
        return discussion_id

    def add_discussion_comment(
        self,
        discussion_id: str,
        body: str,
        author: str = 'orchestrator-bot',
        reply_to_id: Optional[str] = None
    ) -> str:
        """
        Alias for add_comment_to_discussion for convenience

        Returns: Comment ID
        """
        return self.add_comment_to_discussion(discussion_id, body, author, reply_to_id)

    def get_discussion_comments(self, discussion_id: str) -> List[Dict]:
        """Get all comments from a discussion"""
        if discussion_id not in self._discussions:
            return []

        return self._discussions[discussion_id]['comments']['nodes']

    def assert_comment_posted(self, body_substring: str) -> bool:
        """Assert that a comment containing text was posted"""
        for call in self._call_log:
            if call['type'] == 'graphql' and 'addDiscussionComment' in call.get('query', ''):
                if body_substring in call['variables'].get('body', ''):
                    return True
        return False

    def assert_graphql_called(self, query_substring: str) -> bool:
        """Assert that a GraphQL query was made"""
        for call in self._call_log:
            if call['type'] == 'graphql' and query_substring in call.get('query', ''):
                return True
        return False

    def get_call_count(self, call_type: Optional[str] = None) -> int:
        """Count API calls of a specific type"""
        if call_type:
            return sum(1 for call in self._call_log if call['type'] == call_type)
        return len(self._call_log)

    def clear_call_log(self):
        """Clear the call log (useful between test cases)"""
        self._call_log = []

    def reset(self):
        """Reset all mock data"""
        self._discussions = {}
        self._comments = {}
        self._projects = {}
        self._call_log = []


class MockGitHubIntegration:
    """Mock GitHubIntegration service"""

    def __init__(self, github_app: MockGitHubApp):
        self.github_app = github_app
        self._posted_comments = []

    async def post_agent_output(
        self,
        context: Dict[str, Any],
        comment: str,
        reply_to_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Mock posting agent output to GitHub"""
        self._posted_comments.append({
            'context': context,
            'comment': comment,
            'reply_to_id': reply_to_id,
            'timestamp': datetime.now().isoformat()
        })

        if context.get('workspace_type') == 'discussions':
            discussion_id = context.get('discussion_id')
            if discussion_id:
                comment_id = self.github_app.add_comment_to_discussion(
                    discussion_id, comment, reply_to_id=reply_to_id
                )
                return {'success': True, 'comment_id': comment_id}

        return {'success': True}

    async def post_issue_comment(
        self,
        issue_number: int,
        comment: str,
        repository: str
    ):
        """Mock posting to issue"""
        self._posted_comments.append({
            'issue_number': issue_number,
            'comment': comment,
            'repository': repository,
            'timestamp': datetime.now().isoformat()
        })

    def get_issue_details(
        self,
        repository: str,
        issue_number: int,
        org: str
    ) -> Dict[str, Any]:
        """Mock get issue details"""
        return {
            'number': issue_number,
            'title': f'Test Issue #{issue_number}',
            'body': 'Test issue body',
            'state': 'open',
            'labels': []
        }

    def get_posted_comments(self) -> List[Dict]:
        """Get all posted comments for assertions"""
        return self._posted_comments


class MockAgentExecutor:
    """Mock agent executor for fast testing"""

    def __init__(self):
        self._responses = {}
        self._executions = []

    def set_response(self, agent_name: str, output: str, success: bool = True):
        """Configure mock agent response"""
        self._responses[agent_name] = {
            'output': output,
            'success': success,
            'responses_list': None  # Not using list mode
        }

    def set_responses(self, agent_name: str, outputs: List[str]):
        """
        Configure multiple sequential responses for an agent

        Each time the agent is called, the next response in the list is returned.
        Useful for testing multiple iterations.
        """
        self._responses[agent_name] = {
            'output': None,
            'success': True,
            'responses_list': outputs.copy(),
            'responses_index': 0
        }

    async def execute_agent(
        self,
        agent_name: str,
        task_context: Dict[str, Any],
        project_name: str
    ) -> Dict[str, Any]:
        """Execute mock agent (returns instantly)"""
        self._executions.append({
            'agent': agent_name,
            'context': task_context,
            'project': project_name,
            'timestamp': datetime.now().isoformat()
        })

        response_config = self._responses.get(agent_name, {
            'output': f"Mock output from {agent_name}",
            'success': True,
            'responses_list': None
        })

        # Check if using sequential responses
        if response_config.get('responses_list'):
            responses = response_config['responses_list']
            index = response_config.get('responses_index', 0)

            if index < len(responses):
                output = responses[index]
                response_config['responses_index'] = index + 1
            else:
                # Exhausted list, use last response
                output = responses[-1] if responses else f"Mock output from {agent_name}"

            return {
                'output': output,
                'success': response_config.get('success', True),
                'duration_ms': 100
            }
        else:
            # Single response mode
            return {
                'output': response_config.get('output', f"Mock output from {agent_name}"),
                'success': response_config.get('success', True),
                'duration_ms': 100
            }

    def get_executions(self) -> List[Dict]:
        """Get all agent executions for assertions"""
        return self._executions

    def assert_agent_executed(self, agent_name: str) -> bool:
        """Assert that an agent was executed"""
        return any(exec['agent'] == agent_name for exec in self._executions)

    def get_execution_count(self, agent_name: Optional[str] = None) -> int:
        """Count agent executions"""
        if agent_name:
            return sum(1 for exec in self._executions if exec['agent'] == agent_name)
        return len(self._executions)

    def call_count(self, agent_name: str) -> int:
        """Alias for get_execution_count for convenience"""
        return self.get_execution_count(agent_name)

    def reset(self):
        """Reset execution log"""
        self._executions = []
        self._responses = {}
