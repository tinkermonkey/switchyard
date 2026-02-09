"""
GitHub Discussions API Integration

Provides high-level interface for working with GitHub Discussions using GraphQL.
Uses GitHub App authentication for proper bot identity.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from services.github_app import github_app
from services.github_api_client import get_github_client

logger = logging.getLogger(__name__)


class GitHubDiscussions:
    """GitHub Discussions API client"""

    def __init__(self):
        """Initialize discussions client"""
        self.app = github_app
        self.client = get_github_client()

    def _execute_graphql(self, query: str, variables: Dict[str, Any] = None) -> Optional[Dict]:
        """Execute GraphQL query using App or Client fallback"""
        if self.app.enabled:
            return self.app.graphql_request(query, variables)
        
        # Fallback to client (PAT)
        success, result = self.client.graphql(query, variables)
        if success:
            return result
        
        logger.error(f"GraphQL execution failed: {result}")
        return None

    def get_discussion_comments(self, owner: str, repo: str, discussion_id: str) -> List[Dict]:
        """
        Get comments for a discussion by node ID
        
        Args:
            owner: Repository owner
            repo: Repository name
            discussion_id: Discussion node ID (e.g. D_kwD...)
            
        Returns: List of comment objects
        """
        query = """
        query($discussionId: ID!) {
          node(id: $discussionId) {
            ... on Discussion {
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
        """
        
        result = self._execute_graphql(query, {'discussionId': discussion_id})
        
        if result and 'node' in result and 'comments' in result['node']:
            return result['node']['comments']['nodes']
            
        logger.error(f"Failed to get comments for discussion {discussion_id}")
        return []

    def get_repository_id(self, owner: str, repo: str) -> Optional[str]:
        """Get repository ID (node ID) for GraphQL operations"""
        query = """
        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            id
          }
        }
        """

        result = self._execute_graphql(query, {
            'owner': owner,
            'repo': repo
        })

        if result and 'repository' in result:
            return result['repository']['id']

        logger.error(f"Failed to get repository ID for {owner}/{repo}")
        return None

    def create_discussion(self, owner: str, repo: str, category_id: str,
                         title: str, body: str, repository_id: Optional[str] = None) -> Optional[Dict]:
        """
        Create a new discussion

        Args:
            owner: Repository owner
            repo: Repository name
            category_id: Discussion category ID
            title: Discussion title
            body: Discussion body (markdown)
            repository_id: Optional repository node ID (fetched if not provided)

        Returns: Dict with 'id', 'number', and 'url' if successful, None otherwise
        """
        # Get repository ID if not provided
        if not repository_id:
            repository_id = self.get_repository_id(owner, repo)
            if not repository_id:
                return None

        repo_id = repository_id

        # Create discussion
        mutation = """
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
        """

        result = self._execute_graphql(mutation, {
            'repositoryId': repo_id,
            'categoryId': category_id,
            'title': title,
            'body': body
        })

        if result and 'createDiscussion' in result:
            discussion_data = result['createDiscussion']['discussion']
            discussion_id = discussion_data['id']
            discussion_number = discussion_data['number']
            discussion_url = discussion_data['url']
            logger.info(f"Created discussion #{discussion_number}: {title}")
            return {
                'id': discussion_id,
                'number': discussion_number,
                'url': discussion_url
            }

        logger.error("Failed to create discussion")
        return None

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
        mutation = """
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
        """

        result = self._execute_graphql(mutation, {
            'discussionId': discussion_id,
            'body': body,
            'replyToId': reply_to_id
        })

        if result and 'addDiscussionComment' in result:
            comment_id = result['addDiscussionComment']['comment']['id']
            logger.info(f"Added comment to discussion {discussion_id} (reply_to: {reply_to_id})")
            return comment_id

        logger.error(f"Failed to add comment to discussion {discussion_id}. Result: {result}")
        return None

    def get_discussions_updated_at(self, discussion_ids: List[str]) -> Dict[str, Optional[str]]:
        """
        Batch-fetch updatedAt timestamps for multiple discussions in a single GraphQL call.

        Uses aliased node() lookups to fetch all timestamps at once instead of
        one API call per discussion.

        Args:
            discussion_ids: List of discussion node IDs

        Returns:
            Dict mapping discussion_id to updatedAt string, or None if discussion was deleted/not found
        """
        if not discussion_ids:
            return {}

        # Build aliased query: { d0: node(id: "...") { ... on Discussion { id updatedAt } } ... }
        fragments = []
        for i, did in enumerate(discussion_ids):
            fragments.append(
                f'd{i}: node(id: "{did}") {{ ... on Discussion {{ id updatedAt }} }}'
            )

        query = "{ " + " ".join(fragments) + " }"

        result = self._execute_graphql(query)
        if not result:
            logger.warning("Batch discussion updatedAt query failed, returning empty results")
            return {did: None for did in discussion_ids}

        # Map results back to discussion IDs
        updated_at_map = {}
        for i, did in enumerate(discussion_ids):
            alias = f'd{i}'
            node_data = result.get(alias)
            if node_data and 'updatedAt' in node_data:
                updated_at_map[did] = node_data['updatedAt']
            else:
                updated_at_map[did] = None

        return updated_at_map

    def get_discussion(self, discussion_id: str) -> Optional[Dict]:
        """Get discussion details by node ID"""
        query = """
        query($discussionId: ID!) {
          node(id: $discussionId) {
            ... on Discussion {
              id
              number
              title
              body
              url
              createdAt
              updatedAt
              author {
                login
              }
              category {
                id
                name
              }
            }
          }
        }
        """

        result = self._execute_graphql(query, {
            'discussionId': discussion_id
        })

        if result and 'node' in result:
            return result['node']

        # Don't log error - already logged by graphql_request
        # Most common case is NOT_FOUND for deleted discussions
        return None

    def get_discussion_by_number(self, owner: str, repo: str, number: int) -> Optional[Dict]:
        """Get discussion details by number"""
        query = """
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
        """

        result = self._execute_graphql(query, {
            'owner': owner,
            'repo': repo,
            'number': number
        })

        if result and 'repository' in result and result['repository']['discussion']:
            return result['repository']['discussion']

        logger.error(f"Failed to get discussion #{number}")
        return None

    def list_discussions(self, owner: str, repo: str,
                        category_id: Optional[str] = None,
                        first: int = 20) -> List[Dict]:
        """
        List discussions in a repository

        Args:
            owner: Repository owner
            repo: Repository name
            category_id: Optional category ID to filter by
            first: Number of discussions to return

        Returns: List of discussion objects
        """
        query = """
        query($owner: String!, $repo: String!, $first: Int!, $categoryId: ID) {
          repository(owner: $owner, name: $repo) {
            discussions(first: $first, categoryId: $categoryId, orderBy: {field: UPDATED_AT, direction: DESC}) {
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
        """

        result = self._execute_graphql(query, {
            'owner': owner,
            'repo': repo,
            'first': first,
            'categoryId': category_id
        })

        if result and 'repository' in result:
            return result['repository']['discussions']['nodes']

        logger.error(f"Failed to list discussions for {owner}/{repo}")
        return []

    def get_discussion_categories(self, owner: str, repo: str) -> List[Dict]:
        """Get all discussion categories for a repository"""
        query = """
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
        """

        result = self._execute_graphql(query, {
            'owner': owner,
            'repo': repo
        })

        if result and 'repository' in result:
            return result['repository']['discussionCategories']['nodes']

        logger.debug(f"Could not get discussion categories for {owner}/{repo} (GitHub App not configured)")
        return []

    def find_category_by_name(self, owner: str, repo: str, category_name: str) -> Optional[str]:
        """Find category ID by name"""
        categories = self.get_discussion_categories(owner, repo)
        for category in categories:
            if category['name'].lower() == category_name.lower():
                return category['id']
        return None

    def convert_discussion_to_issue(self, discussion_id: str) -> Optional[int]:
        """
        Convert a discussion to an issue

        Returns: Issue number if successful, None otherwise
        """
        # Note: This is not directly supported in GraphQL yet
        # We'll need to create an issue manually and link it
        logger.warning("Discussion to issue conversion not yet implemented via API")
        return None

    def search_discussions_for_mentions(self, owner: str, repo: str,
                                       since: Optional[datetime] = None) -> List[Dict]:
        """
        Search for discussions with @orchestrator-bot mentions

        Args:
            owner: Repository owner
            repo: Repository name
            since: Only return discussions updated since this time

        Returns: List of discussions with mentions
        """
        discussions = self.list_discussions(owner, repo, first=50)

        # Filter for mentions in comments
        mentioned_discussions = []
        for discussion in discussions:
            # Check if updated recently
            if since:
                updated_at = datetime.fromisoformat(discussion['updatedAt'].replace('Z', '+00:00'))
                if updated_at < since:
                    continue

            # Get full discussion with comments to check for mentions
            full_discussion = self.get_discussion_by_number(owner, repo, discussion['number'])
            if not full_discussion:
                continue

            # Check all comments for @orchestrator-bot mention
            has_mention = False
            for comment in full_discussion.get('comments', {}).get('nodes', []):
                if '@orchestrator-bot' in comment.get('body', ''):
                    has_mention = True
                    break
                # Also check replies
                for reply in comment.get('replies', {}).get('nodes', []):
                    if '@orchestrator-bot' in reply.get('body', ''):
                        has_mention = True
                        break

            if has_mention:
                mentioned_discussions.append(full_discussion)

        return mentioned_discussions
