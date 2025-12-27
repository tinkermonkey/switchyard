import asyncio
import os
import sys
import logging
from services.github_app import github_app
from dateutil import parser as date_parser

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    org = "tinkermonkey"
    repo = "documentation_robotics"
    discussion_number = 20
    
    print(f"Debugging discussion #{discussion_number} for {org}/{repo}...")
    
    # Get the specific discussion with all comments and replies
    print(f"\nFetching discussion #{discussion_number}...")
    query_discussion = """
    query($org: String!, $repo: String!, $number: Int!) {
      repository(owner: $org, name: $repo) {
        discussion(number: $number) {
          id
          title
          number
          body
          author { login }
          createdAt
          comments(first: 100) {
            nodes {
              databaseId
              id
              body
              author { login }
              createdAt
              replyTo {
                databaseId
                id
              }
              replies(first: 100) {
                nodes {
                  databaseId
                  id
                  body
                  author { login }
                  createdAt
                  replyTo {
                    databaseId
                    id
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    result = github_app.graphql_request(query_discussion, {'org': org, 'repo': repo, 'number': discussion_number})
    
    if result and 'repository' in result and result['repository']['discussion']:
        disc = result['repository']['discussion']
        print(f"\nDiscussion #{disc['number']}: {disc['title']}")
        print(f"Author: {disc['author']['login']}")
        print(f"Created: {disc['createdAt']}")
        print(f"\nBody:\n{disc['body'][:200]}...\n")
        
        comments = disc['comments']['nodes']
        print(f"Total comments: {len(comments)}")
        
        # Look for specific comments
        target_comment_ids = [15181664, 15185580]
        
        for comment in comments:
            if comment['databaseId'] in target_comment_ids:
                print(f"\n{'='*80}")
                print(f"FOUND Comment {comment['databaseId']} (ID: {comment['id']})")
                print(f"Author: {comment['author']['login']}")
                print(f"Created: {comment['createdAt']}")
                print(f"ReplyTo: {comment.get('replyTo', 'None')}")
                print(f"Body:\n{comment['body'][:500]}")
                print(f"{'='*80}\n")
            
            # Check replies
            replies = comment['replies']['nodes']
            for reply in replies:
                if reply['databaseId'] in target_comment_ids:
                    print(f"\n{'='*80}")
                    print(f"FOUND Reply {reply['databaseId']} (ID: {reply['id']})")
                    print(f"Author: {reply['author']['login']}")
                    print(f"Created: {reply['createdAt']}")
                    print(f"ReplyTo: {reply.get('replyTo', 'None')}")
                    print(f"Body:\n{reply['body'][:500]}")
                    print(f"Parent Comment ID: {comment['databaseId']}")
                    print(f"{'='*80}\n")
        
        # Print all comments and replies in thread order
        print("\n\nFull thread structure:")
        for i, comment in enumerate(comments):
            print(f"\n{i+1}. Comment {comment['databaseId']} by {comment['author']['login']}")
            print(f"   Created: {comment['createdAt']}")
            print(f"   ReplyTo: {comment.get('replyTo', 'None')}")
            print(f"   Body preview: {comment['body'][:100]}...")
            
            replies = comment['replies']['nodes']
            if replies:
                print(f"   {len(replies)} replies:")
                for j, reply in enumerate(replies):
                    print(f"   {i+1}.{j+1}. Reply {reply['databaseId']} by {reply['author']['login']}")
                    print(f"        Created: {reply['createdAt']}")
                    print(f"        ReplyTo: {reply.get('replyTo', 'None')}")
                    print(f"        Body preview: {reply['body'][:100]}...")
    else:
        print("Error fetching discussion.")

if __name__ == "__main__":
    asyncio.run(main())
