import os
from services.github_app import github_app

# Get the specific comment
query = """
query {
  node(id: "DC_kwDOQaznN84A57hR") {
    ... on DiscussionComment {
      id
      databaseId
      author { login }
      body
      createdAt
      discussion {
        id
        number
      }
      replyTo {
        id
        databaseId
      }
      isAnswer
    }
  }
}
"""

result = github_app.graphql_request(query, {})
import json
print(json.dumps(result, indent=2))
