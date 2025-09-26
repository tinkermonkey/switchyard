# Condense issue

## Github issue: $ARGUMENTS

IMPORTANT: Your task is to condense the conversation in the provided github issue and make sure that the issue body contains the most recent agreed upon scope for a ticket based on the comments.

- Use the get_issue_comments tool from the mcp__github-official__ mcp server to load the issue and all comments for the issue
- Use the update_issue tool from the mcp__github-official__ mcp server to update the body of the issue
- Use the list_sub_issues tool from the mcp__github-official__ mcp server to list the sub-issues for the provided issue
- Use the add_sub_issue tool from the mcp__github-official__ mcp server to update the parent of an issue if one is not properly linked
- Use the search_issues tool from the mcp__github-official__ mcp server to find any issues that should be sub-issues of this issue but which are not properly linked currently, but which reference this issue
- When you've condensed the issue and updated the body, add a simple comment to the issue that says "updated issue based on comments"

## Conversation Condensing Process

You are condensing the conversation in the comments for this issue into a final set of requirements.

Make sure that the complete agreed upon scope and details discussed in the comments is represented in your updated body.

1. Review the issues which reference this issue and make sure they are linked as sub-issues if they should be

2. Review all comments and identify the agreed upon scope and direction

3. Condense that agreed upon scope and direction into your final output