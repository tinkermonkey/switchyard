#!/usr/bin/env python3
from services.github_app import github_app

# Convert database ID to node ID
# Discussion comment IDs in URLs are database IDs, need to query by node ID
# Let's try to find all recent comments across all discussions

query = """
query {
  repository(owner: "tinkermonkey", name: "documentation_robotics") {
    discussions(first: 5, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        title
        updatedAt
        comments(first: 10) {
          totalCount
          nodes {
            databaseId
            id
            author { login }
            createdAt
          }
        }
      }
    }
  }
}
"""

result = github_app.graphql_request(query, {})
print(f"Result type: {type(result)}")
print(f"Result: {result}")
if result and 'repository' in result:
    discussions = result['repository']['discussions']['nodes']
    for disc in discussions:
        print(f"\nDiscussion #{disc['number']}: {disc['title']}")
        print(f"  Updated: {disc['updatedAt']}")
        print(f"  Total comments: {disc['comments']['totalCount']}")
        for comment in disc['comments']['nodes']:
            print(f"    - DB ID: {comment.get('databaseId')}, Node ID: {comment['id']}")
            print(f"      {comment['author']['login']} at {comment['createdAt']}")
else:
    print("No result")
