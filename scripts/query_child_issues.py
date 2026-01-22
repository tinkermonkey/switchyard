#!/usr/bin/env python3
"""
Helper script to query child issues of a parent issue using GitHub's GraphQL API.

Usage:
    python scripts/query_child_issues.py <org> <repo> <issue_number>

Example:
    python scripts/query_child_issues.py tinkermonkey codetoreum 90
"""

import sys
import json
from services.github_api_client import get_github_client


def query_child_issues(owner: str, repo: str, issue_number: int):
    """
    Query all child issues (sub-issues) of a parent issue using GitHub's GraphQL API.

    Args:
        owner: GitHub organization or user
        repo: Repository name
        issue_number: Parent issue number

    Returns:
        List of child issue data dictionaries
    """
    github_client = get_github_client()

    # Use the same GraphQL query as in feature_branch_manager.py
    query = '''
    query($owner: String!, $repo: String!, $issueNumber: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $issueNumber) {
          number
          title
          state
          subIssues(first: 100) {
            totalCount
            nodes {
              number
              title
              state
              url
              closedAt
            }
          }
        }
      }
    }
    '''

    variables = {
        "owner": owner,
        "repo": repo,
        "issueNumber": issue_number
    }

    print(f"Querying child issues for {owner}/{repo}#{issue_number}...")
    print()

    success, result = github_client.graphql(query, variables)

    if not success:
        print(f"ERROR: GraphQL query failed: {result}")
        return []

    # Extract issue data
    issue_data = result.get('repository', {}).get('issue', {})

    if not issue_data:
        print(f"ERROR: Issue #{issue_number} not found")
        return []

    print(f"Parent Issue: #{issue_data['number']} - {issue_data['title']}")
    print(f"State: {issue_data['state']}")
    print()

    # Extract sub-issues
    sub_issues_data = issue_data.get('subIssues', {})
    total_count = sub_issues_data.get('totalCount', 0)
    sub_issues = sub_issues_data.get('nodes', [])

    print(f"Child Issues: {total_count} total")
    print("=" * 80)

    if not sub_issues:
        print("No child issues found.")
        return []

    # Display child issues
    for i, issue in enumerate(sub_issues, 1):
        print(f"{i}. Issue #{issue['number']}: {issue['title']}")
        print(f"   State: {issue['state']}")
        print(f"   URL: {issue['url']}")
        if issue.get('closedAt'):
            print(f"   Closed: {issue['closedAt']}")
        print()

    # Summary
    closed_count = sum(1 for issue in sub_issues if issue['state'] == 'CLOSED')
    open_count = total_count - closed_count

    print("=" * 80)
    print(f"Summary: {closed_count} closed, {open_count} open")

    if closed_count == total_count:
        print("✓ ALL child issues are CLOSED")
    else:
        print(f"✗ {open_count} child issue(s) still OPEN")

    return sub_issues


def main():
    if len(sys.argv) != 4:
        print("Usage: python scripts/query_child_issues.py <org> <repo> <issue_number>")
        print("Example: python scripts/query_child_issues.py tinkermonkey codetoreum 90")
        sys.exit(1)

    owner = sys.argv[1]
    repo = sys.argv[2]

    try:
        issue_number = int(sys.argv[3])
    except ValueError:
        print(f"ERROR: Issue number must be an integer, got: {sys.argv[3]}")
        sys.exit(1)

    child_issues = query_child_issues(owner, repo, issue_number)

    # Exit code: 0 if all closed, 1 if any open
    if child_issues:
        all_closed = all(issue['state'] == 'CLOSED' for issue in child_issues)
        sys.exit(0 if all_closed else 1)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
