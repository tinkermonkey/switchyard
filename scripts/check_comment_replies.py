#!/usr/bin/env python3
from services.github_app import github_app

# Query the specific comment for its replies
query = """
query {
  node(id: "DC_kwDOQaznN84A56cW") {
    ... on DiscussionComment {
      id
      author { login }
      body
      createdAt
      replies(first: 10) {
        totalCount
        nodes {
          id
          databaseId
          author { login }
          body
          createdAt
        }
      }
    }
  }
}
"""

result = github_app.graphql_request(query, {})
print(f"Result: {result}")
if result and 'node' in result and result['node']:
    comment = result['node']
    print(f"\nComment {comment['id']} from {comment['author']['login']}")
    print(f"Created: {comment['createdAt']}")
    
    replies = comment.get('replies', {})
    if replies:
        print(f"\nReplies: {replies.get('totalCount', 0)}")
        for reply in replies.get('nodes', []):
            print(f"  - DB ID: {reply.get('databaseId')}, Node ID: {reply['id']}")
            print(f"    {reply['author']['login']} at {reply['createdAt']}")
            print(f"    {reply['body'][:100]}...")
    else:
        print("No replies field in response")
else:
    print("No comment found")
