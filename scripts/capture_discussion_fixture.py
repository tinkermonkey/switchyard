#!/usr/bin/env python3
"""
Capture a GitHub Discussion as a test fixture

This script fetches a discussion with all comments and replies,
and saves it as JSON for use in unit tests.
"""

import os
import sys
import json
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.github_app import github_app


def capture_discussion(org: str, repo: str, discussion_number: int, output_file: str):
    """
    Capture a discussion as a test fixture

    Args:
        org: GitHub organization
        repo: Repository name
        discussion_number: Discussion number
        output_file: Path to save JSON fixture
    """
    print(f"Fetching discussion #{discussion_number} from {org}/{repo}...")

    # Query to get full discussion with comments and replies
    query = """
    query($org: String!, $repo: String!, $number: Int!) {
      repository(owner: $org, name: $repo) {
        discussion(number: $number) {
          id
          number
          title
          body
          createdAt
          author {
            login
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

    variables = {
        'org': org,
        'repo': repo,
        'number': discussion_number
    }

    result = github_app.graphql_request(query, variables)

    if not result or 'repository' not in result:
        print(f"ERROR: Failed to fetch discussion: {result}")
        sys.exit(1)

    discussion = result['repository']['discussion']

    # Print summary
    comment_count = len(discussion['comments']['nodes'])
    total_replies = sum(
        len(comment['replies']['nodes'])
        for comment in discussion['comments']['nodes']
    )

    print(f"\nDiscussion: {discussion['title']}")
    print(f"Comments: {comment_count}")
    print(f"Replies: {total_replies}")
    print(f"Total items: {comment_count + total_replies}")

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Save to file
    with open(output_file, 'w') as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved to: {output_file}")
    print(f"File size: {os.path.getsize(output_file) / 1024:.1f} KB")

    # Print comment timeline for test planning
    print("\n=== Comment Timeline ===")
    for i, comment in enumerate(discussion['comments']['nodes']):
        author = comment['author']['login'] if comment['author'] else 'unknown'
        reply_count = len(comment['replies']['nodes'])
        timestamp = comment['createdAt']

        # Check if it's an agent comment
        body_preview = comment['body'][:100].replace('\n', ' ')
        is_agent = '_Processed by the' in comment['body']
        agent_name = None

        if is_agent:
            # Extract agent name
            import re
            match = re.search(r'_Processed by the (\w+) agent_', comment['body'])
            if match:
                agent_name = match.group(1)

        marker = f"[{agent_name.upper()}]" if agent_name else ""
        print(f"{i:2d}. {timestamp} - {author:20s} {marker:25s} ({reply_count} replies)")
        print(f"    Preview: {body_preview}...")

        # Print replies
        for j, reply in enumerate(comment['replies']['nodes']):
            reply_author = reply['author']['login'] if reply['author'] else 'unknown'
            reply_preview = reply['body'][:80].replace('\n', ' ')
            print(f"    └─ Reply {j+1}: {reply_author} - {reply_preview}...")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Capture GitHub Discussion as test fixture')
    parser.add_argument('--org', default='austinsand', help='GitHub organization')
    parser.add_argument('--repo', default='context-studio', help='Repository name')
    parser.add_argument('--discussion', type=int, required=True, help='Discussion number')
    parser.add_argument('--output', default=None, help='Output file (default: tests/fixtures/discussion_N.json)')

    args = parser.parse_args()

    # Default output path
    if args.output is None:
        output = f'tests/fixtures/discussion_{args.discussion}.json'
    else:
        output = args.output

    capture_discussion(args.org, args.repo, args.discussion, output)
