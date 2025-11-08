#!/usr/bin/env python3
"""
Quick script to add a manual review filter based on learnings.

Usage:
    python scripts/add_review_filter.py \\
        --agent code_reviewer \\
        --category project_conventions \\
        --severity high \\
        --pattern "Code violates CLAUDE.md conventions" \\
        --samples "Issue #102 created markdown docs despite CLAUDE.md forbidding it" \\
        --action highlight

Actions:
    - suppress: Don't report this pattern (low-value noise)
    - highlight: Emphasize checking this pattern (high-value)
    - adjust_severity: Change severity level
"""
import sys
import asyncio
import argparse
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.review_filter_manager import get_review_filter_manager
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(
        description='Add a manual review filter based on learnings',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:

  # Highlight CLAUDE.md compliance checks
  python scripts/add_review_filter.py \\
      --agent code_reviewer \\
      --category project_conventions \\
      --severity high \\
      --pattern "Code violates project CLAUDE.md conventions" \\
      --samples "Issue #102 created documentation files but CLAUDE.md says avoid creating docs" \\
      --action highlight

  # Suppress low-value noise
  python scripts/add_review_filter.py \\
      --agent code_reviewer \\
      --category code_style \\
      --severity low \\
      --pattern "Missing docstrings in private helper methods" \\
      --samples "Repeatedly flagged but developers don't add them" \\
      --action suppress
        """
    )

    parser.add_argument('--agent', required=True,
                       choices=['requirements_reviewer', 'design_reviewer', 'code_reviewer',
                               'test_reviewer', 'qa_reviewer'],
                       help='Agent to apply filter to')
    parser.add_argument('--category', required=True,
                       help='Finding category (e.g., project_conventions, code_style, security)')
    parser.add_argument('--severity', required=True,
                       choices=['critical', 'high', 'medium', 'low'],
                       help='Severity level')
    parser.add_argument('--pattern', required=True,
                       help='Description of the pattern to filter')
    parser.add_argument('--samples', required=True,
                       help='Example finding that matches this pattern')
    parser.add_argument('--action', required=True,
                       choices=['suppress', 'highlight', 'adjust_severity'],
                       help='Action to take on this pattern')
    parser.add_argument('--confidence', type=float, default=0.90,
                       help='Confidence level (0.0-1.0, default: 0.90)')
    parser.add_argument('--reason', default=None,
                       help='Reason for this filter (auto-generated if not provided)')
    parser.add_argument('--from-severity', default=None,
                       help='For adjust_severity: original severity level')
    parser.add_argument('--to-severity', default=None,
                       help='For adjust_severity: new severity level')

    args = parser.parse_args()

    # Validate severity adjustment args
    if args.action == 'adjust_severity':
        if not args.from_severity or not args.to_severity:
            parser.error("--from-severity and --to-severity required for adjust_severity action")

    # Generate reason if not provided
    if args.reason is None:
        if args.action == 'suppress':
            args.reason = "Low-value finding typically ignored by developers"
        elif args.action == 'highlight':
            args.reason = "High-value pattern that catches real issues"
        elif args.action == 'adjust_severity':
            args.reason = f"Severity adjustment from {args.from_severity} to {args.to_severity}"

    logger.info("=" * 80)
    logger.info("CREATING REVIEW FILTER")
    logger.info("=" * 80)
    logger.info(f"Agent: {args.agent}")
    logger.info(f"Category: {args.category}")
    logger.info(f"Severity: {args.severity}")
    logger.info(f"Action: {args.action}")
    logger.info(f"Pattern: {args.pattern}")
    logger.info(f"Reason: {args.reason}")
    logger.info(f"Confidence: {args.confidence}")

    try:
        filter_manager = get_review_filter_manager()

        filter_data = {
            'agent': args.agent,
            'category': args.category,
            'severity': args.severity,
            'pattern_description': args.pattern,
            'reason_ignored': args.reason,
            'sample_findings': [args.samples],
            'action': args.action,
            'confidence': args.confidence,
            'sample_size': 1,  # Manual, so sample_size=1
            'active': True,
            'manual_override': True  # Mark as manually created
        }

        # Add severity adjustment fields if applicable
        if args.action == 'adjust_severity':
            filter_data['from_severity'] = args.from_severity
            filter_data['to_severity'] = args.to_severity

        filter_id = await filter_manager.create_filter(filter_data)

        logger.info("=" * 80)
        logger.info("FILTER CREATED SUCCESSFULLY!")
        logger.info("=" * 80)
        logger.info(f"Filter ID: {filter_id}")
        logger.info("")
        logger.info("Next time the agent runs, this filter will be automatically loaded")
        logger.info("and injected into the agent's prompt.")
        logger.info("")
        logger.info("To view all filters:")
        logger.info("  docker-compose exec elasticsearch curl -s 'http://localhost:9200/review-filters/_search?pretty'")
        logger.info("")
        logger.info("To deactivate this filter:")
        logger.info(f"  filter_manager.deactivate_filter('{filter_id}')")

    except Exception as e:
        logger.error(f"Error creating filter: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
