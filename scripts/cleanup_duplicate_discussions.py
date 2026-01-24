#!/usr/bin/env python3
"""
Cleanup duplicate discussions created by the orchestrator.

Usage:
  python scripts/cleanup_duplicate_discussions.py --dry-run    # Analyze only
  python scripts/cleanup_duplicate_discussions.py --delete     # Actually delete
"""

import subprocess
import json
import yaml
from datetime import datetime
from typing import List, Dict, Optional
import sys
import os
import argparse

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def gh_graphql(query: str, variables: Optional[Dict] = None) -> Dict:
    """Call GitHub GraphQL API"""
    cmd = ['gh', 'api', 'graphql', '-f', f'query={query}']
    if variables:
        for key, value in variables.items():
            cmd.extend(['-F', f'{key}={value}'])
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise Exception(f"GraphQL call failed: {result.stderr}")
    return json.loads(result.stdout)


def get_all_discussions(owner: str, repo: str) -> List[Dict]:
    """Get all discussions for a repository"""
    query = """
    query($owner: String!, $repo: String!) {
      repository(owner: $owner, name: $repo) {
        discussions(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes {
            id
            number
            title
            createdAt
            body
            comments(first: 100) {
              totalCount
              nodes {
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
    """
    
    result = gh_graphql(query, {'owner': owner, 'repo': repo})
    return result['data']['repository']['discussions']['nodes']


def find_duplicates(discussions: List[Dict]) -> Dict[str, List[Dict]]:
    """Group discussions by title to find duplicates"""
    by_title = {}
    for disc in discussions:
        title = disc['title'].lower().strip()
        if title not in by_title:
            by_title[title] = []
        by_title[title].append(disc)
    
    return {title: discs for title, discs in by_title.items() if len(discs) > 1}


def delete_discussion(discussion_id: str):
    """Delete a discussion using GraphQL mutation"""
    mutation = """
    mutation($discussionId: ID!) {
      deleteDiscussion(input: {id: $discussionId}) {
        clientMutationId
      }
    }
    """
    
    result = gh_graphql(mutation, {'discussionId': discussion_id})
    return result


def analyze_discussion_content(discussion: Dict) -> Dict:
    """Analyze discussion content"""
    return {
        'id': discussion['id'],
        'number': discussion['number'],
        'title': discussion['title'],
        'created': discussion['createdAt'],
        'body_length': len(discussion.get('body', '')),
        'comment_count': discussion['comments']['totalCount'],
        'has_agent_comments': any(
            'orchestrator' in c.get('author', {}).get('login', '').lower() or
            'claude' in c.get('body', '').lower()
            for c in discussion['comments']['nodes']
        )
    }


def main():
    parser = argparse.ArgumentParser(description='Cleanup duplicate discussions')
    parser.add_argument('--dry-run', action='store_true', help='Analyze only, do not delete')
    parser.add_argument('--delete', action='store_true', help='Actually delete duplicates')
    args = parser.parse_args()
    
    if not args.dry_run and not args.delete:
        print("Error: Must specify --dry-run or --delete")
        sys.exit(1)
    
    projects = [
        {'name': 'codetoreum', 'owner': 'tinkermonkey', 'repo': 'codetoreum'},
        {'name': 'context-studio', 'owner': 'tinkermonkey', 'repo': 'context-studio'},
        {'name': 'documentation_robotics_viewer', 'owner': 'tinkermonkey', 'repo': 'documentation_robotics_viewer'},
        {'name': 'documentation_robotics', 'owner': 'tinkermonkey', 'repo': 'documentation_robotics'},
        {'name': 'utterance_emitter', 'owner': 'tinkermonkey', 'repo': 'utterance_emitter'},
        {'name': 'what_am_i_watching', 'owner': 'tinkermonkey', 'repo': 'what_am_i_watching'},
    ]
    
    print("=" * 80)
    print("DUPLICATE DISCUSSION CLEANUP")
    print("Mode:", "DRY RUN" if args.dry_run else "DELETE")
    print("=" * 80)
    print()
    
    all_duplicates = {}
    
    for project in projects:
        print(f"\n📋 {project['name']} ({project['owner']}/{project['repo']})...")
        
        try:
            discussions = get_all_discussions(project['owner'], project['repo'])
            duplicates = find_duplicates(discussions)
            
            if not duplicates:
                print(f"  ✓ No duplicates")
                continue
            
            print(f"  ⚠️  {len(duplicates)} duplicate groups:")
            
            for title, group in duplicates.items():
                group.sort(key=lambda d: datetime.fromisoformat(d['createdAt'].replace('Z', '+00:00')))
                
                print(f"\n  '{title}'")
                
                for i, disc in enumerate(group):
                    analysis = analyze_discussion_content(disc)
                    marker = "  [KEEP]" if i == 0 else "[DELETE]"
                    print(f"    {marker} #{disc['number']} - {disc['createdAt'][:10]} - "
                          f"Comments: {analysis['comment_count']}, Agent work: {analysis['has_agent_comments']}")
                
                all_duplicates[f"{project['name']}:{title}"] = {
                    'project': project,
                    'group': group
                }
        
        except Exception as e:
            print(f"  ❌ Error: {e}")
    
    if not all_duplicates:
        print("\n✓ No duplicates found!")
        return
    
    total_to_delete = sum(len(v['group']) - 1 for v in all_duplicates.values())
    print(f"\n{'=' * 80}")
    print(f"SUMMARY: {total_to_delete} discussions to delete")
    print("=" * 80)
    
    if args.dry_run:
        print("\n✓ Dry run complete. Use --delete to actually delete.")
        return
    
    # Actually delete
    print("\n🗑️  DELETING DUPLICATES...")
    deleted_count = 0
    
    for key, data in all_duplicates.items():
        project = data['project']
        group = data['group']
        
        for disc in group[1:]:
            try:
                print(f"  Deleting {project['name']} #{disc['number']}...", end=' ')
                delete_discussion(disc['id'])
                print("✓")
                deleted_count += 1
            except Exception as e:
                print(f"❌ {e}")
    
    print(f"\n✓ Deleted {deleted_count} discussions")
    print("\n⚠️  Restart orchestrator to update state files!")


if __name__ == '__main__':
    main()
