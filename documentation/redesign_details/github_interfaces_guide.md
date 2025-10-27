# GitHub Interfaces Integration Guide

**Document Version**: 1.0
**Last Updated**: 2025-10-27

## Overview

This document provides a comprehensive guide to how the Claude Code Agent Orchestrator integrates with GitHub through various interfaces. The orchestrator uses a multi-layered approach combining GitHub CLI, GraphQL API, REST API, and GitHub App authentication to manage development workflows.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Authentication Mechanisms](#authentication-mechanisms)
3. [GitHub CLI (gh) Interface](#github-cli-gh-interface)
4. [GraphQL API Interface](#graphql-api-interface)
5. [REST API Interface](#rest-api-interface)
6. [GitHub Discussions Integration](#github-discussions-integration)
7. [Rate Limiting and Circuit Breaker](#rate-limiting-and-circuit-breaker)
8. [Capability Detection](#capability-detection)
9. [Best Practices and Patterns](#best-practices-and-patterns)

---

## Architecture Overview

The orchestrator uses a centralized GitHub API client pattern with multiple interface adapters:

```
┌─────────────────────────────────────────────────────────────┐
│                    Application Layer                         │
│  (Agents, Services, Pipeline Orchestration)                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│              GitHubAPIClient (Central Hub)                   │
│  • Rate limiting & circuit breaker                           │
│  • Request queuing & throttling                              │
│  • Usage tracking & alarming                                 │
│  • Call tracing & observability                              │
└─┬──────────┬──────────┬──────────┬──────────┬──────────────┘
  │          │          │          │          │
  ▼          ▼          ▼          ▼          ▼
┌────┐   ┌────┐   ┌────────┐  ┌──────┐   ┌──────────┐
│gh  │   │REST│   │GraphQL │  │HTTP  │   │GitHub    │
│CLI │   │API │   │API     │  │Direct│   │App Auth  │
└────┘   └────┘   └────────┘  └──────┘   └──────────┘
  │          │          │          │          │
  └──────────┴──────────┴──────────┴──────────┘
                        │
              ┌─────────▼─────────┐
              │  GitHub Platform  │
              └───────────────────┘
```

### Key Components

- **`services/github_api_client.py`**: Centralized API client with rate limiting
- **`services/github_integration.py`**: High-level GitHub operations
- **`services/github_discussions.py`**: Discussions-specific operations
- **`services/github_app.py`**: GitHub App authentication
- **`services/github_app_auth.py`**: JWT-based token generation
- **`services/github_project_manager.py`**: Project board reconciliation
- **`services/project_monitor.py`**: Board polling and monitoring
- **`services/github_capabilities.py`**: Feature availability tracking

---

## Authentication Mechanisms

### 1. Personal Access Token (PAT)

**Purpose**: Primary authentication method for GitHub CLI operations

**Configuration**:
```bash
export GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"
export GH_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"  # For gh CLI
```

**Authentication Flow**:
```
gh auth status  →  GitHub API  →  Validate token
                                      │
                                      ▼
                              Token valid: Allow operations
                              Token invalid: Fail
```

**Usage**:
- GitHub CLI commands (`gh project`, `gh issue`, `gh pr`)
- REST API calls via `gh api`
- GraphQL queries via `gh api graphql`
- Repository access and project management

**Scopes Required**:
- `repo` - Full repository access
- `project` - Projects v2 read/write
- `read:org` - Organization metadata
- `workflow` - GitHub Actions (if needed)

**Limitations**:
- Comments appear as the authenticated user (not as a bot)
- Rate limit: 5000 requests/hour
- Cannot use GitHub Discussions GraphQL mutations

### 2. GitHub App Authentication

**Purpose**: Bot identity for comments and Discussions access

**Configuration**:
```bash
export GITHUB_APP_ID="123456"
export GITHUB_APP_INSTALLATION_ID="12345678"
export GITHUB_APP_PRIVATE_KEY_PATH="~/.orchestrator/orchestrator-bot.pem"
```

**Authentication Flow**:
```
1. Generate JWT using private key (RS256)
   ├─ iat: issued at time
   ├─ exp: expires in 10 minutes
   └─ iss: GitHub App ID

2. Exchange JWT for installation token
   POST /app/installations/{installation_id}/access_tokens
   Authorization: Bearer {JWT}
   │
   ▼
   Installation token (valid 1 hour)

3. Use installation token for API calls
   Authorization: Bearer {installation_token}
```

**Implementation** (`services/github_app_auth.py:70-89`):
```python
def generate_jwt(self) -> str:
    """Generate a JWT for GitHub App authentication"""
    now = int(time.time())
    payload = {
        'iat': now,
        'exp': now + (10 * 60),  # 10 minutes
        'iss': self.app_id
    }
    return jwt.encode(payload, self.private_key, algorithm='RS256')

def get_installation_token(self, force_refresh: bool = False) -> Optional[str]:
    """Get installation access token (cached, auto-refreshed)"""
    # Token caching with 5-minute safety margin
    if not force_refresh and self.installation_token and self.token_expires_at:
        if datetime.now(timezone.utc) < self.token_expires_at:
            return self.installation_token

    # Generate new token...
```

**Benefits**:
- Comments appear as "orchestrator-bot[bot]"
- Better rate limits (5000 req/hour per installation)
- Access to GitHub Discussions GraphQL API
- Proper bot identity separation

**Limitations**:
- Requires private key management
- More complex setup than PAT
- Token refresh logic needed

---

## GitHub CLI (gh) Interface

### Overview

The GitHub CLI is used extensively for project management, repository operations, and quick GitHub interactions. All CLI operations are routed through the centralized API client for rate limiting.

### Implementation Location

**File**: `services/github_api_client.py:738-816`

### Core Method

```python
def gh_cli(self, cmd: List[str], retries: int = 0) -> Tuple[bool, Any]:
    """
    Execute a GitHub CLI command with circuit breaker awareness.

    Args:
        cmd: List of command parts, e.g., ['gh', 'project', 'create', ...]
        retries: Current retry count (internal use)

    Returns:
        Tuple of (success, result) where result is parsed JSON or raw output
    """
```

### Common Usage Patterns

#### 1. Project Board Management

**Creating Project Boards** (`services/github_project_manager.py:196-214`):
```python
cmd = [
    'gh', 'project', 'create',
    '--owner', org,
    '--title', f"{project_name} - {description}",
    '--format', 'json'
]
success, result = github_client.gh_cli(cmd)
# Returns: {'id': '...', 'number': 123, 'node_id': '...'}
```

**Linking Projects to Repositories** (`services/github_project_manager.py:223-235`):
```python
cmd = [
    'gh', 'project', 'link', str(project_number),
    '--owner', org,
    '--repo', repo
]
success, result = github_client.gh_cli(cmd)
```

**Listing Project Fields** (`services/github_project_manager.py:276-281`):
```python
cmd = ['gh', 'project', 'field-list', str(project_number),
       '--owner', org, '--format', 'json']
success, result = github_client.gh_cli(cmd)
```

#### 2. Issue Operations

**Creating Issues** (`services/github_integration.py:372-385`):
```python
cmd = ['gh', 'issue', 'create',
       '--title', title,
       '--body', body,
       '--repo', f"{org}/{repo}"]

if labels:
    for label in labels:
        cmd.extend(['--label', label])

result = subprocess.run(cmd, capture_output=True, text=True,
                       check=True, env=self._get_gh_env())
issue_url = result.stdout.strip()
issue_number = issue_url.split('/')[-1]
```

**Viewing Issues** (`services/github_integration.py:163-172`):
```python
cmd = ['gh', 'issue', 'view', str(issue_number),
       '--json', 'comments',
       '--repo', f"{org}/{repo}"]
result = subprocess.run(cmd, capture_output=True, text=True,
                       check=True, env=self._get_gh_env())
data = json.loads(result.stdout)
```

**Adding Labels** (`services/github_integration.py:344-355`):
```python
cmd = ['gh', 'issue', 'edit', str(issue_number),
       '--add-label', label,
       '--repo', f"{org}/{repo}"]
subprocess.run(cmd, capture_output=True, text=True,
              check=True, env=self._get_gh_env())
```

#### 3. Pull Request Operations

**Creating Pull Requests** (`services/github_integration.py:514-533`):
```python
cmd = [
    'gh', 'pr', 'create',
    '--repo', f"{owner}/{repo}",
    '--base', 'main',
    '--head', branch,
    '--title', title,
    '--body', body
]

if draft:
    cmd.append('--draft')

result = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=30, env=self._get_gh_env())
pr_url = result.stdout.strip()
pr_number = int(pr_url.rstrip('/').split('/')[-1])
```

**Marking PR Ready** (`services/github_integration.py:584-600`):
```python
cmd = [
    'gh', 'pr', 'ready', str(pr_number),
    '--repo', f"{owner}/{repo}"
]
```

#### 4. Authentication Operations

**Checking Auth Status** (`services/github_capabilities.py:45-50`):
```python
pat_result = subprocess.run(
    ['gh', 'auth', 'status'],
    capture_output=True,
    text=True
)
pat_authenticated = pat_result.returncode == 0
```

### Environment Variable Management

**Token Injection** (`services/github_integration.py:34-45`):
```python
def _get_gh_env(self) -> dict:
    """Get environment variables for gh CLI"""
    env = os.environ.copy()

    if self.auth_type == "github_app":
        token = self.github_app.get_installation_token()
        if token:
            env['GH_TOKEN'] = token
            # Remove GITHUB_TOKEN to prevent conflicts
            env.pop('GITHUB_TOKEN', None)

    return env
```

### CLI Command Patterns

| Command Pattern | Purpose | Output Format |
|----------------|---------|---------------|
| `gh project create` | Create project board | JSON with `id`, `number`, `node_id` |
| `gh project field-list` | List project fields | JSON with field definitions |
| `gh project link` | Link project to repo | Success/error message |
| `gh issue create` | Create new issue | Issue URL |
| `gh issue view --json` | View issue details | JSON with issue data |
| `gh pr create` | Create pull request | PR URL |
| `gh pr ready` | Mark PR as ready | Success message |
| `gh auth status` | Check authentication | Exit code (0=success) |

---

## GraphQL API Interface

### Overview

GraphQL is used for complex queries requiring nested data, particularly for GitHub Projects v2, Discussions, and rate limit tracking. The orchestrator uses both direct GraphQL calls via the GitHub App and GraphQL through the GitHub CLI.

### Implementation Location

**File**: `services/github_api_client.py:232-357`

### Core Method

```python
def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None,
            retries: int = 0) -> Tuple[bool, Any]:
    """
    Execute a GraphQL query with rate limiting and error handling.

    Args:
        query: GraphQL query string
        variables: Query variables (optional)
        retries: Current retry count (internal use)

    Returns:
        Tuple of (success, response_data)
    """
```

### Query Execution Methods

#### 1. Via GitHub CLI (with PAT)

```python
# Build command for GraphQL
cmd = ['gh', 'api', 'graphql']

if variables:
    # Build JSON payload with query and variables
    payload = {
        "query": query,
        "variables": variables
    }
    input_data = json.dumps(payload)
    cmd.extend(['--input', '-'])
else:
    # For simple queries without variables, use -F flag
    cmd.extend(['-F', f'query={query}'])

result = subprocess.run(cmd, input=input_data, capture_output=True,
                       text=True, timeout=30)
```

#### 2. Via GitHub App (for Discussions)

**File**: `services/github_app.py:92-136`

```python
def graphql_request(self, query: str, variables: Dict[str, Any] = None) -> Optional[Dict]:
    """Execute a GraphQL request using GitHub App authentication"""
    token = self.get_installation_token()

    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    payload = {'query': query}
    if variables:
        payload['variables'] = variables

    response = requests.post(
        'https://api.github.com/graphql',
        headers=headers,
        json=payload
    )
    return response.json().get('data')
```

### Common GraphQL Queries

#### 1. Rate Limit Tracking

**Query** (`services/github_api_client.py:913-921`):
```graphql
{
  rateLimit {
    limit
    remaining
    resetAt
  }
}
```

**Usage**:
- Executed automatically every 5 minutes
- Updated from response headers on each query
- Triggers throttling at 80%, 90%, 95% usage

#### 2. Repository ID Lookup

**Query** (`services/github_discussions.py:25-36`):
```graphql
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    id
  }
}
```

**Purpose**: Get repository node ID for GraphQL mutations

#### 3. Discussion Creation

**Mutation** (`services/github_discussions.py:68-83`):
```graphql
mutation($repositoryId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
  createDiscussion(input: {
    repositoryId: $repositoryId
    categoryId: $categoryId
    title: $title
    body: $body
  }) {
    discussion {
      id
      number
      url
    }
  }
}
```

**Variables**:
```python
{
    'repositoryId': repo_id,
    'categoryId': category_id,
    'title': title,
    'body': body
}
```

#### 4. Discussion Comments

**Mutation** (`services/github_discussions.py:113-127`):
```graphql
mutation($discussionId: ID!, $body: String!, $replyToId: ID) {
  addDiscussionComment(input: {
    discussionId: $discussionId
    body: $body
    replyToId: $replyToId
  }) {
    comment {
      id
      url
      createdAt
    }
  }
}
```

**Features**:
- Top-level comments: `replyToId: null`
- Threaded replies: `replyToId: parent_comment_id`

#### 5. Discussion Retrieval

**Query** (`services/github_discussions.py:181-220`):
```graphql
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    discussion(number: $number) {
      id
      title
      body
      createdAt
      updatedAt
      author {
        login
      }
      category {
        id
        name
      }
      comments(first: 100) {
        nodes {
          id
          body
          createdAt
          author {
            login
          }
          replies(first: 50) {
            nodes {
              id
              body
              createdAt
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
```

**Features**:
- Nested comment structure
- Pagination support (first: N)
- Author information
- Reply threading

#### 6. Discussion Categories

**Query** (`services/github_discussions.py:290-303`):
```graphql
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    discussionCategories(first: 20) {
      nodes {
        id
        name
        emoji
        description
      }
    }
  }
}
```

**Usage**: Find category IDs for creating discussions

#### 7. Discussion Search for Mentions

**Query** (`services/github_discussions.py:248-273`):
```graphql
query($owner: String!, $repo: String!, $first: Int!, $categoryId: ID) {
  repository(owner: $owner, name: $repo) {
    discussions(first: $first, categoryId: $categoryId,
                orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        id
        number
        title
        body
        createdAt
        updatedAt
        author {
          login
        }
        category {
          id
          name
        }
        comments {
          totalCount
        }
      }
    }
  }
}
```

### Rate Limit Extraction

**From GraphQL Response** (`services/github_api_client.py:611-647`):
```python
def _update_rate_limit_from_graphql_response(self, response: Dict[str, Any]):
    """Extract rate limit from GraphQL response"""
    rl = None

    # Try extensions.cost.rateLimit first (from cost analysis)
    if 'extensions' in response and 'cost' in response['extensions']:
        cost = response['extensions']['cost']
        if 'rateLimit' in cost:
            rl = cost['rateLimit']

    # Fall back to data.rateLimit (if queried directly)
    if not rl and 'data' in response and 'rateLimit' in response['data']:
        rl = response['data']['rateLimit']

    if rl:
        self.rate_limit.remaining = rl.get('remaining', self.rate_limit.remaining)
        self.rate_limit.limit = rl.get('limit', self.rate_limit.limit)
        reset_at = rl.get('resetAt')
        if reset_at:
            self.rate_limit.reset_time = datetime.fromisoformat(
                reset_at.replace('Z', '+00:00')
            )
```

---

## REST API Interface

### Overview

The REST API is used for simpler operations like issue comments, PR operations, and repository management. REST calls are preferred when GraphQL is overkill or when the operation is straightforward.

### Implementation Location

**File**: `services/github_api_client.py:359-463`

### Core Method

```python
def rest(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None,
         retries: int = 0) -> Tuple[bool, Any]:
    """
    Execute a REST API call with rate limiting and error handling.

    Args:
        method: HTTP method ('GET', 'POST', 'PATCH', 'DELETE')
        endpoint: GitHub REST endpoint (e.g., '/repos/owner/repo/issues/1')
        data: Optional request body for POST/PATCH
        retries: Current retry count (internal use)

    Returns:
        Tuple of (success, response_data)
    """
```

### HTTP Direct Method (for POST/PATCH with bodies)

**File**: `services/github_api_client.py:465-587`

```python
def http_request(self, method: str, url: str, data: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None,
                 retries: int = 0) -> Tuple[bool, Any]:
    """Execute an HTTP request to GitHub API with rate limiting"""

    # Prepare headers with authentication
    request_headers = headers or {}
    if 'Authorization' not in request_headers:
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if token:
            request_headers['Authorization'] = f'token {token}'

    # Execute request based on method
    if method.upper() == 'POST':
        response = requests.post(url, json=data, headers=request_headers, timeout=30)
    # ... other methods
```

### Common REST Operations

#### 1. Issue Comments

**Post Comment** (`services/github_integration.py:47-71`):
```python
async def post_issue_comment(self, issue_number: int, comment: str,
                             repo: Optional[str] = None) -> Dict[str, Any]:
    """Post a comment to a GitHub issue using REST API"""
    repo_name = repo or self.repo_name
    endpoint = f"/repos/{self.github_org}/{repo_name}/issues/{issue_number}/comments"

    success, response = get_github_client().rest(
        method='POST',
        endpoint=endpoint,
        data={'body': comment}
    )

    return {
        'success': success,
        'html_url': response.get('html_url'),
        'id': response.get('id')
    }
```

**Get Issue Details** (`services/github_integration.py:139-158`):
```python
async def get_issue_details(self, issue_number: int,
                            repo: Optional[str] = None) -> Dict[str, Any]:
    """Get issue details using REST API"""
    repo_name = repo or self.repo_name
    endpoint = f"/repos/{self.github_org}/{repo_name}/issues/{issue_number}"

    success, response = get_github_client().rest(
        method='GET',
        endpoint=endpoint
    )

    return response if success else {}
```

**Get Feedback Comments** (`services/github_integration.py:249-317`):
```python
async def get_feedback_comments(self, issue_number: int,
                                repo: Optional[str] = None,
                                since_timestamp: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get comments that mention @orchestrator-bot"""
    repo_name = repo or self.repo_name
    endpoint = f"/repos/{self.github_org}/{repo_name}/issues/{issue_number}/comments"

    success, response = get_github_client().rest(
        method='GET',
        endpoint=endpoint
    )

    # Filter for @orchestrator-bot mentions
    feedback_comments = []
    for comment in response:
        if '@orchestrator-bot' in comment.get('body', ''):
            # Check timestamp filter if provided
            if since_timestamp:
                comment_time = parser.parse(comment['created_at'])
                since_time = parser.parse(since_timestamp)
                if comment_time <= since_time:
                    continue

            feedback_comments.append({
                'id': comment['id'],
                'body': comment['body'],
                'author': comment['user']['login'],
                'created_at': comment['created_at'],
                'is_bot': comment['user']['type'] == 'Bot'
            })

    return feedback_comments
```

#### 2. Pull Request Operations

**Get PR Details** (`services/github_integration.py:319-338`):
```python
async def get_pr_details(self, pr_number: int,
                        repo: Optional[str] = None) -> Dict[str, Any]:
    """Get pull request details using REST API"""
    repo_name = repo or self.repo_name
    endpoint = f"/repos/{self.github_org}/{repo_name}/pulls/{pr_number}"

    success, response = get_github_client().rest(
        method='GET',
        endpoint=endpoint
    )

    return response if success else {}
```

**Create PR Review** (`services/github_integration.py:100-137`):
```python
async def create_pr_review(self, pr_number: int, review_type: str,
                          body: str, repo: Optional[str] = None) -> Dict[str, Any]:
    """Create a formal PR review"""
    repo_name = repo or self.repo_name
    endpoint = f"/repos/{self.github_org}/{repo_name}/pulls/{pr_number}/reviews"

    # Map review_type to GitHub API event
    event_map = {
        'approve': 'APPROVE',
        'request-changes': 'REQUEST_CHANGES',
        'comment': 'COMMENT'
    }
    event = event_map.get(review_type, review_type.upper())

    success, response = get_github_client().rest(
        method='POST',
        endpoint=endpoint,
        data={
            'body': body,
            'event': event
        }
    )

    return {'success': success, 'review_id': response.get('id')}
```

#### 3. Branch Operations

**Delete Branch** (`services/github_integration.py:613-641`):
```python
async def delete_branch(self, branch_name: str) -> bool:
    """Delete a remote branch"""
    repo_arg = f"{self.repo_owner}/{self.repo_name}"

    cmd = [
        'gh', 'api',
        f'repos/{repo_arg}/git/refs/heads/{branch_name}',
        '-X', 'DELETE'
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode == 0
```

### REST vs GraphQL Decision Tree

```
Need to perform GitHub operation?
│
├─ Complex nested data needed?
│  └─ YES → Use GraphQL
│     Examples: Projects v2, Discussions with comments/replies
│
├─ Simple CRUD on single resource?
│  └─ YES → Use REST API
│     Examples: Issue comments, PR details, branch operations
│
├─ Discussions operations?
│  └─ YES → Use GraphQL (requires GitHub App)
│
├─ Rate limit tracking?
│  └─ YES → Use GraphQL (rateLimit query)
│
└─ Project board management?
   └─ YES → Use GitHub CLI (`gh project`)
```

---

## GitHub Discussions Integration

### Overview

GitHub Discussions provide a forum-like interface for collaborative conversation. The orchestrator uses Discussions for threaded agent collaboration, design discussions, and question-answering workflows.

### Implementation Location

**File**: `services/github_discussions.py`

### Key Features

1. **Discussion Creation**: Create new discussion threads
2. **Comment Posting**: Add top-level comments
3. **Reply Threading**: Reply to existing comments (nested threads)
4. **Category Management**: Organize discussions by category
5. **Mention Detection**: Search for @orchestrator-bot mentions

### Discussion Operations

#### 1. Create Discussion

```python
def create_discussion(self, owner: str, repo: str, category_id: str,
                     title: str, body: str,
                     repository_id: Optional[str] = None) -> Optional[str]:
    """
    Create a new discussion

    Returns: discussion ID if successful, None otherwise
    """
```

**Flow**:
```
1. Get repository ID (if not provided)
   └─ GraphQL: repository(owner, name) { id }

2. Create discussion mutation
   └─ GraphQL: createDiscussion(input: {...})

3. Return discussion ID and number
```

#### 2. Add Discussion Comment

```python
def add_discussion_comment(self, discussion_id: str, body: str,
                          reply_to_id: Optional[str] = None) -> Optional[str]:
    """
    Add a comment to a discussion

    Args:
        discussion_id: The discussion node ID
        body: Comment body (markdown)
        reply_to_id: Optional comment ID to reply to (creates nested thread)

    Returns: comment ID if successful, None otherwise
    """
```

**Top-level comment**:
```python
comment_id = discussions.add_discussion_comment(
    discussion_id="DI_kwDOABC123",
    body="This is a top-level comment"
)
```

**Threaded reply**:
```python
reply_id = discussions.add_discussion_comment(
    discussion_id="DI_kwDOABC123",
    body="This is a reply to a comment",
    reply_to_id="DC_kwDOABC456"  # Parent comment ID
)
```

#### 3. Get Discussion with Comments

```python
def get_discussion_by_number(self, owner: str, repo: str,
                             number: int) -> Optional[Dict]:
    """Get discussion details by number (includes comments and replies)"""
```

**Response Structure**:
```json
{
  "id": "DI_kwDOABC123",
  "number": 42,
  "title": "Discussion Title",
  "body": "Discussion body...",
  "category": {
    "id": "DIC_kwDOABC789",
    "name": "General"
  },
  "comments": {
    "nodes": [
      {
        "id": "DC_kwDOABC456",
        "body": "Top-level comment",
        "author": {"login": "user1"},
        "replies": {
          "nodes": [
            {
              "id": "DC_kwDOABC789",
              "body": "Nested reply",
              "author": {"login": "user2"}
            }
          ]
        }
      }
    ]
  }
}
```

#### 4. Search for Mentions

```python
def search_discussions_for_mentions(self, owner: str, repo: str,
                                   since: Optional[datetime] = None) -> List[Dict]:
    """
    Search for discussions with @orchestrator-bot mentions

    Returns: List of discussions with mentions
    """
```

**Algorithm**:
```
1. List recent discussions (first: 50, orderBy: UPDATED_AT DESC)

2. For each discussion:
   ├─ Check if updated since filter time
   ├─ Get full discussion with comments
   └─ Search for @orchestrator-bot in:
      ├─ Top-level comments
      └─ Nested replies

3. Return discussions with mentions found
```

#### 5. Get Discussion Categories

```python
def get_discussion_categories(self, owner: str, repo: str) -> List[Dict]:
    """Get all discussion categories for a repository"""
```

**Usage Example**:
```python
categories = discussions.get_discussion_categories("myorg", "myrepo")
# [
#   {'id': 'DIC_...', 'name': 'General', 'emoji': '💬'},
#   {'id': 'DIC_...', 'name': 'Ideas', 'emoji': '💡'},
#   {'id': 'DIC_...', 'name': 'Q&A', 'emoji': '❓'}
# ]

# Find category by name
category_id = discussions.find_category_by_name("myorg", "myrepo", "Ideas")
```

### Integration with Agent Workflows

**Workspace Type Detection** (`services/github_integration.py:401-429`):
```python
async def post_agent_output(self, context: Dict[str, Any], comment: str,
                           reply_to_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Post agent output to the appropriate workspace (Issues or Discussions)

    Args:
        context: Task context containing workspace information
        comment: Formatted comment body
        reply_to_id: Optional comment ID to reply to (for discussions)
    """
    workspace_type = context.get('workspace_type', 'issues')
    discussion_id = context.get('discussion_id')

    # If we have a discussion_id, use discussions
    if discussion_id or workspace_type in ['discussions', 'hybrid']:
        if discussion_id:
            return await self._post_discussion_comment(context, comment, reply_to_id)
        else:
            # Fall back to issues
            return await self._post_issue_comment(context, comment)
    else:
        return await self._post_issue_comment(context, comment)
```

### Discussion Comment Structure

**Agent Output Format**:
```markdown
# Idea Research

---

[Agent output content here...]

---
_Generated by Orchestrator Bot 🤖_
_Processed by the idea_researcher agent_
```

**Threaded Replies**:
- Initial output: Top-level comment
- Feedback response: Reply to feedback comment
- Revision output: Reply to reviewer comment

---

## Rate Limiting and Circuit Breaker

### Overview

The orchestrator implements sophisticated rate limiting to prevent API quota exhaustion and ensure sustainable operation under GitHub's API rate limits.

### Rate Limit Specifications

| Authentication | Rate Limit | Window | Resource |
|---------------|------------|--------|----------|
| PAT | 5,000 requests | 1 hour | REST API |
| PAT | 5,000 points | 1 hour | GraphQL |
| GitHub App | 5,000 requests | 1 hour per installation | REST API |
| GitHub App | 5,000 points | 1 hour per installation | GraphQL |

### Architecture

```
┌────────────────────────────────────────────────────────────┐
│              GitHubAPIClient Rate Limiting                  │
├────────────────────────────────────────────────────────────┤
│  1. Rate Limit Tracking                                    │
│     ├─ Current: 3245/5000 remaining (35.1% used)           │
│     ├─ Reset time: 2025-10-27 14:30:00 UTC                │
│     └─ Last updated: from API response headers             │
│                                                            │
│  2. Adaptive Throttling                                    │
│     ├─ 80% used: Log warning, continue                    │
│     ├─ 90% used: Wait 10 seconds before request           │
│     └─ 95% used: Wait 30 seconds before request           │
│                                                            │
│  3. Circuit Breaker                                        │
│     ├─ CLOSED: Normal operation                           │
│     ├─ OPEN: Rate limit hit, reject requests              │
│     └─ HALF_OPEN: Testing recovery                        │
│                                                            │
│  4. Request Queuing                                        │
│     ├─ Min interval: 100ms between requests               │
│     ├─ Backoff multiplier: 1.0x (increases on errors)     │
│     └─ Max backoff: 60 seconds                            │
└────────────────────────────────────────────────────────────┘
```

### Implementation

#### 1. Rate Limit Status Tracking

**File**: `services/github_api_client.py:32-96`

```python
class GitHubRateLimitStatus:
    """Track GitHub API rate limit status"""

    def __init__(self):
        self.limit = 5000  # Points per hour
        self.remaining = 5000
        self.reset_time: Optional[datetime] = None
        self.resource_type = "graphql"  # or "rest"

    def get_percentage_used(self) -> float:
        """Get percentage of rate limit used (0-100)"""
        if self.limit == 0:
            return 0
        return ((self.limit - self.remaining) / self.limit) * 100

    def get_time_until_reset(self) -> Optional[float]:
        """Get seconds until rate limit resets"""
        if not self.reset_time:
            return None
        now = datetime.now()
        if now >= self.reset_time:
            return 0
        return (self.reset_time - now).total_seconds()
```

#### 2. Circuit Breaker

**File**: `services/github_api_client.py:99-186`

```python
class GitHubBreaker:
    """Circuit breaker for GitHub API rate limits"""

    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Rate limit hit, reject requests
    HALF_OPEN = "half_open"  # Testing if rate limit reset

    def trip(self, reset_time: Optional[datetime] = None):
        """Open the breaker due to rate limit"""
        self.state = self.OPEN
        self.opened_at = datetime.now()
        self.reset_time = reset_time or (datetime.now() + timedelta(hours=1))
        logger.error(
            f"🔴 GITHUB API CIRCUIT BREAKER OPENED - Rate limit exceeded. "
            f"Will reset at {self.reset_time}"
        )

    def check_and_close(self) -> bool:
        """Check if rate limit reset and close breaker"""
        if self.reset_time and datetime.now() >= self.reset_time:
            self.state = self.HALF_OPEN
            logger.warning("🟡 GITHUB API BREAKER HALF-OPEN - Testing...")
            return False
        return self.state == self.CLOSED

    def close(self):
        """Close the breaker (rate limit recovered)"""
        self.state = self.CLOSED
        logger.info("🟢 GITHUB API BREAKER CLOSED - Rate limit recovered")
```

#### 3. Adaptive Throttling

**File**: `services/github_api_client.py:254-266`

```python
# Check usage and apply adaptive throttling
usage_percent = self.rate_limit.get_percentage_used()

if usage_percent > 95:
    wait_time = 30  # Heavy backoff at 95%+ usage
    logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - throttling (waiting {wait_time}s)")
    time.sleep(wait_time)
elif usage_percent > 90:
    wait_time = 10
    logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - backing off (waiting {wait_time}s)")
    time.sleep(wait_time)
elif usage_percent > 80:
    logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - approaching limit")
```

#### 4. Request Backoff

**File**: `services/github_api_client.py:589-601`

```python
def _apply_backoff(self):
    """Apply exponential backoff based on recent failures"""
    with self.lock:
        now = time.time()
        time_since_last = now - self.last_request_time
        min_wait = self.min_request_interval * self.backoff_multiplier

        if time_since_last < min_wait:
            wait_time = min_wait - time_since_last
            logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
            time.sleep(wait_time)

        self.last_request_time = time.time()
```

#### 5. Rate Limit Update from API

**From GraphQL Response** (`services/github_api_client.py:611-647`):
```python
def _update_rate_limit_from_graphql_response(self, response: Dict[str, Any]):
    """Extract rate limit from GraphQL response"""
    # Try extensions.cost.rateLimit (from cost analysis)
    if 'extensions' in response and 'cost' in response['extensions']:
        rl = response['extensions']['cost'].get('rateLimit')
        if rl:
            self.rate_limit.remaining = rl.get('remaining')
            self.rate_limit.limit = rl.get('limit')
            reset_at = rl.get('resetAt')
            if reset_at:
                self.rate_limit.reset_time = datetime.fromisoformat(
                    reset_at.replace('Z', '+00:00')
                )
```

**From HTTP Headers** (`services/github_api_client.py:670-690`):
```python
def _update_rate_limit_from_http_headers(self, headers: Dict[str, str]):
    """Extract rate limit from HTTP response headers"""
    if 'x-ratelimit-limit' in headers:
        self.rate_limit.limit = int(headers['x-ratelimit-limit'])
    if 'x-ratelimit-remaining' in headers:
        self.rate_limit.remaining = int(headers['x-ratelimit-remaining'])
    if 'x-ratelimit-reset' in headers:
        reset_timestamp = int(headers['x-ratelimit-reset'])
        self.rate_limit.reset_time = datetime.fromtimestamp(reset_timestamp)
```

#### 6. Automatic Rate Limit Checking

**Background Thread** (`services/github_api_client.py:986-1009`):
```python
def _start_rate_limit_checker(self):
    """Start background thread to check rate limits every 5 minutes"""
    def check_rate_limits():
        while True:
            try:
                time.sleep(300)  # Check every 5 minutes

                if self._fetch_rate_limit_from_api():
                    logger.info(
                        f"📊 GitHub API Rate Limit Check: "
                        f"{self.rate_limit.remaining}/{self.rate_limit.limit} remaining "
                        f"({self.rate_limit.get_percentage_used():.1f}% used)"
                    )

            except Exception as e:
                logger.debug(f"Error in rate limit checker: {e}")

    thread = Thread(target=check_rate_limits, daemon=True)
    thread.start()
```

### Usage Alarms

**File**: `services/github_api_client.py:710-736`

```python
def alarm_if_needed(self):
    """Check if we should alarm based on rate limit usage"""
    usage = self.rate_limit.get_percentage_used()
    remaining = self.rate_limit.remaining

    if remaining <= 100:
        logger.critical(
            f"🚨 CRITICAL: GitHub API rate limit critically low! "
            f"Only {remaining} points remaining"
        )
    elif remaining <= 250:
        logger.error(
            f"🔴 WARNING: GitHub API rate limit low! "
            f"Only {remaining} points remaining"
        )
    elif usage >= 95:
        logger.warning(f"⚠️  GitHub API usage at 95%+")
    elif usage >= 90:
        logger.warning(f"⚠️  GitHub API usage at 90%")
    elif usage >= 80:
        logger.warning(f"ℹ️  GitHub API usage at 80%")
```

### Monitoring

**Status Endpoint** (accessed via observability server):
```bash
curl http://localhost:5001/health
```

**Response**:
```json
{
  "healthy": true,
  "checks": {
    "github": {
      "healthy": true,
      "rate_limit": {
        "remaining": 3245,
        "limit": 5000,
        "percentage_used": 35.1,
        "reset_time": "2025-10-27T14:30:00Z",
        "time_until_reset": 1850
      },
      "breaker": {
        "state": "closed",
        "is_open": false
      }
    }
  }
}
```

---

## Capability Detection

### Overview

The orchestrator dynamically detects available GitHub features based on authentication configuration, allowing it to gracefully degrade functionality when certain auth methods are unavailable.

### Implementation Location

**File**: `services/github_capabilities.py`

### Capability Types

```python
class GitHubCapability(Enum):
    """Capabilities that may or may not be available"""
    PAT_AUTH = "pat_authentication"
    GITHUB_APP_AUTH = "github_app_authentication"
    REPO_ACCESS = "repository_access"
    PROJECTS_V2 = "projects_v2_access"
    GRAPHQL_FULL = "graphql_full_access"
    GRAPHQL_LIMITED = "graphql_limited_access"
    DISCUSSIONS = "discussions_access"
    ISSUES = "issues_access"
```

### Capability Matrix

| Capability | Requires PAT | Requires GitHub App | Description |
|-----------|--------------|---------------------|-------------|
| `PAT_AUTH` | ✅ | ❌ | Personal Access Token authentication |
| `GITHUB_APP_AUTH` | ❌ | ✅ | GitHub App authentication |
| `REPO_ACCESS` | ✅ | ❌ | Repository read/write access |
| `PROJECTS_V2` | ✅ | ❌ | GitHub Projects v2 management |
| `GRAPHQL_LIMITED` | ✅ | ❌ | GraphQL via gh CLI |
| `GRAPHQL_FULL` | ❌ | ✅ | Full GraphQL API access |
| `DISCUSSIONS` | ❌ | ✅ | GitHub Discussions mutations |
| `ISSUES` | ✅ | ❌ | Issues read/write |

### Detection Logic

**File**: `services/github_capabilities.py:34-79`

```python
def check_capabilities(self) -> Dict[str, any]:
    """Check all GitHub capabilities and store results"""
    from services.github_app import github_app
    import subprocess

    # Check PAT authentication
    pat_result = subprocess.run(
        ['gh', 'auth', 'status'],
        capture_output=True,
        text=True
    )
    pat_authenticated = pat_result.returncode == 0

    # Check GitHub App
    github_app_enabled = github_app.enabled

    # Determine capabilities
    self._capabilities = {
        GitHubCapability.PAT_AUTH: pat_authenticated,
        GitHubCapability.GITHUB_APP_AUTH: github_app_enabled,
        GitHubCapability.REPO_ACCESS: pat_authenticated,
        GitHubCapability.PROJECTS_V2: pat_authenticated,
        GitHubCapability.ISSUES: pat_authenticated,
        GitHubCapability.DISCUSSIONS: github_app_enabled,
        GitHubCapability.GRAPHQL_FULL: github_app_enabled,
        GitHubCapability.GRAPHQL_LIMITED: pat_authenticated,
    }

    # Build warnings
    self._warnings = []
    if not pat_authenticated:
        self._warnings.append(
            "CRITICAL: PAT authentication failed - orchestrator cannot function"
        )
    if not github_app_enabled:
        self._warnings.append(
            "GitHub App not configured - discussions and advanced GraphQL unavailable"
        )

    return {
        'capabilities': {cap.value: enabled for cap, enabled in self._capabilities.items()},
        'warnings': self._warnings
    }
```

### Usage Pattern

**Checking Capabilities**:
```python
from services.github_capabilities import github_capabilities

# Check if Discussions are available
if github_capabilities.has_capability(GitHubCapability.DISCUSSIONS):
    # Create discussion
    discussions.create_discussion(...)
else:
    # Fall back to issue
    github.create_issue(...)
```

**Requiring Capabilities**:
```python
# Require capability for operation
if not github_capabilities.require_capability(
    GitHubCapability.DISCUSSIONS,
    operation="creating discussion for agent collaboration"
):
    logger.error("Cannot proceed without Discussions access")
    return
```

### Graceful Degradation

**Workspace Type Selection**:
```python
# Determine workspace type based on capabilities
if github_capabilities.has_capability(GitHubCapability.DISCUSSIONS):
    workspace_type = 'discussions'  # Preferred for threaded collaboration
elif github_capabilities.has_capability(GitHubCapability.ISSUES):
    workspace_type = 'issues'  # Fallback to issues
else:
    raise RuntimeError("No GitHub workspace available")
```

---

## Best Practices and Patterns

### 1. Always Use Centralized Client

**❌ DON'T**:
```python
# Direct subprocess call - bypasses rate limiting
result = subprocess.run(['gh', 'api', 'graphql', '-f', f'query={query}'])
```

**✅ DO**:
```python
# Use centralized client with rate limiting
github_client = get_github_client()
success, response = github_client.graphql(query, variables)
```

### 2. Handle Rate Limit Errors

**❌ DON'T**:
```python
# Ignore rate limit errors
success, response = github_client.graphql(query)
if not success:
    raise Exception("Query failed")
```

**✅ DO**:
```python
# Check for rate limit errors and handle gracefully
success, response = github_client.graphql(query)
if not success:
    if response.get('error') == 'rate_limited':
        logger.warning("Rate limit hit, circuit breaker activated")
        # Queue task for retry or wait
    else:
        logger.error(f"Query failed: {response}")
```

### 3. Use Appropriate Interface

**Decision Tree**:
```python
# Complex nested data (Projects v2, Discussions)
if need_nested_data:
    use_graphql()

# Simple CRUD operations
elif simple_operation:
    use_rest_api()

# Project board management
elif project_board_operation:
    use_gh_cli('gh project ...')

# Discussions (requires GitHub App)
elif discussions_operation:
    if github_capabilities.has_capability(GitHubCapability.DISCUSSIONS):
        use_graphql_with_github_app()
    else:
        fallback_to_issues()
```

### 4. Track API Calls

**Enable Call Tracing**:
```python
# In github_api_client.py
TRACE_API_CALLS = True

# Calls will be logged with stack trace:
# 📍 Call stack for graphql:
#   └─ github_project_manager:reconcile_project() [line 52]
#      success = await self._reconcile_pipeline_board(...)
```

**Track Operations**:
```python
# Track operations that make indirect API calls
github_client = get_github_client()
github_client.track_gh_operation('gh_pr_create', 'Created PR #123 for feature XYZ')
```

### 5. Handle Workspace Types

**Detect Workspace**:
```python
async def post_agent_output(self, context: Dict[str, Any], comment: str):
    """Post to appropriate workspace"""
    workspace_type = context.get('workspace_type', 'issues')
    discussion_id = context.get('discussion_id')

    # Prefer discussion if available
    if discussion_id or workspace_type == 'discussions':
        return await self._post_discussion_comment(context, comment)
    else:
        return await self._post_issue_comment(context, comment)
```

### 6. Use GitHub App for Bot Identity

**❌ DON'T** (comments appear as user):
```python
# Using PAT - comments show as authenticated user
cmd = ['gh', 'issue', 'comment', str(issue_number), '--body', body]
subprocess.run(cmd, env={'GH_TOKEN': pat_token})
```

**✅ DO** (comments appear as bot):
```python
# Using GitHub App - comments show as "orchestrator-bot[bot]"
github_integration = GitHubIntegration()
await github_integration.post_issue_comment(issue_number, body)
# Uses GitHub App token if configured, falls back to PAT
```

### 7. Monitor Rate Limits

**Check Status**:
```bash
# Via observability server
curl http://localhost:5001/health | jq '.checks.github.rate_limit'
```

**Response**:
```json
{
  "remaining": 3245,
  "limit": 5000,
  "percentage_used": 35.1,
  "reset_time": "2025-10-27T14:30:00Z",
  "time_until_reset": 1850
}
```

**Log Analysis**:
```bash
# Check for rate limit warnings
docker-compose logs orchestrator | grep "GitHub API usage"

# Check circuit breaker status
docker-compose logs orchestrator | grep "CIRCUIT BREAKER"
```

### 8. Retry Transient Errors

**Built-in Retry Logic**:
```python
# Automatic retries with exponential backoff
def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None,
            retries: int = 0) -> Tuple[bool, Any]:

    # Execute request...
    if result.returncode == 1:
        # Exponential backoff on transient errors
        if retries < 3:
            wait_time = (2 ** retries) * 2  # 2s, 4s, 8s
            logger.info(f"Retrying after {wait_time}s (attempt {retries + 1}/3)")
            time.sleep(wait_time)
            return self.graphql(query, variables, retries + 1)
```

### 9. Use Pagination for Large Result Sets

**GraphQL Pagination**:
```graphql
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    discussions(first: 100, after: $cursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        title
      }
    }
  }
}
```

**Python Implementation**:
```python
def get_all_discussions(owner: str, repo: str) -> List[Dict]:
    """Fetch all discussions with pagination"""
    all_discussions = []
    cursor = None

    while True:
        # Query with cursor
        variables = {'owner': owner, 'repo': repo, 'cursor': cursor}
        success, response = github_client.graphql(query, variables)

        # Collect results
        discussions = response['repository']['discussions']['nodes']
        all_discussions.extend(discussions)

        # Check if more pages
        page_info = response['repository']['discussions']['pageInfo']
        if not page_info['hasNextPage']:
            break

        cursor = page_info['endCursor']

    return all_discussions
```

### 10. Graceful Fallback

**Example: Discussions → Issues**:
```python
async def create_collaboration_workspace(self, title: str, body: str):
    """Create workspace with fallback"""

    # Try Discussions first (preferred)
    if github_capabilities.has_capability(GitHubCapability.DISCUSSIONS):
        try:
            discussions = GitHubDiscussions()
            category_id = discussions.find_category_by_name(
                owner, repo, "Agent Collaboration"
            )
            discussion_id = discussions.create_discussion(
                owner, repo, category_id, title, body
            )
            return {'workspace_type': 'discussions', 'discussion_id': discussion_id}
        except Exception as e:
            logger.warning(f"Discussions creation failed: {e}, falling back to Issues")

    # Fallback to Issues
    github = GitHubIntegration()
    result = await github.create_issue_from_agent(
        title=title,
        body=body,
        labels=['agent-collaboration']
    )
    return {'workspace_type': 'issues', 'issue_number': result['issue_number']}
```

---

## Summary

The orchestrator's GitHub integration is built on a multi-layered architecture:

1. **Centralized API Client**: All GitHub operations flow through `GitHubAPIClient` for rate limiting
2. **Multiple Interfaces**: GitHub CLI, GraphQL, and REST APIs used based on operation requirements
3. **Dual Authentication**: PAT for general operations, GitHub App for bot identity
4. **Rate Limiting**: Adaptive throttling, circuit breaker, and automatic recovery
5. **Capability Detection**: Graceful degradation when features are unavailable
6. **Observability**: Call tracing, usage tracking, and comprehensive logging

This design ensures:
- **Resilience**: Circuit breaker prevents rate limit exhaustion
- **Efficiency**: Right tool for each job (CLI, GraphQL, REST)
- **Flexibility**: Graceful degradation when auth methods unavailable
- **Visibility**: Comprehensive tracking and monitoring

---

## File Reference

| File | Purpose | Key Classes/Functions |
|------|---------|---------------------|
| `services/github_api_client.py` | Centralized API client | `GitHubAPIClient`, `GitHubBreaker`, `GitHubRateLimitStatus` |
| `services/github_integration.py` | High-level GitHub ops | `GitHubIntegration`, `AgentCommentFormatter` |
| `services/github_discussions.py` | Discussions operations | `GitHubDiscussions` |
| `services/github_app.py` | GitHub App client | `GitHubApp` |
| `services/github_app_auth.py` | JWT authentication | `GitHubAppAuth` |
| `services/github_project_manager.py` | Project board reconciliation | `GitHubProjectManager` |
| `services/project_monitor.py` | Board monitoring | `ProjectMonitor` |
| `services/github_capabilities.py` | Feature detection | `GitHubCapabilities`, `GitHubCapability` |

---

## Additional Resources

- [GitHub REST API Documentation](https://docs.github.com/en/rest)
- [GitHub GraphQL API Documentation](https://docs.github.com/en/graphql)
- [GitHub CLI Manual](https://cli.github.com/manual/)
- [GitHub Apps Documentation](https://docs.github.com/en/apps)
- [GitHub Discussions API](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions)
