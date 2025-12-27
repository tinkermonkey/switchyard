#!/usr/bin/env python3
from services.github_app import github_app

# Try querying by discussion number instead of node ID
query = """
query {
  repository(owner: "tinkermonkey", name: "documentation_robotics") {
    discussion(number: 20) {
      id
      title
      comments(first: 100) {
        totalCount
        nodes {
          id
          author { login }
          createdAt
          body
          replyTo { id }
        }
      }
    }
  }
}
"""

result = github_app.graphql_request(query, {})
if result and 'repository' in result and result['repository']:
    discussion = result['repository']['discussion']
    comments = discussion['comments']
    print(f"Discussion: {discussion['title']}")
    print(f"Total count: {comments['totalCount']}")
    print(f"Returned: {len(comments['nodes'])}")
    print()
    for i, c in enumerate(comments['nodes']):
        reply_to_obj = c.get('replyTo')
        reply_to = reply_to_obj.get('id') if reply_to_obj else None
        body_preview = c.get('body', '')[:80].replace('\n', ' ')
        print(f"{i+1}. {c['id']} from {c['author']['login']} at {c['createdAt']}")
        print(f"   replyTo={reply_to}")
        print(f"   body: {body_preview}...")
        print()
else:
    print("No result")
    print(result)
