#!/usr/bin/env python3
"""
Checkpoint Inspector

Shows checkpoint files for a pipeline, verifies recovery state, and visualizes
stage completion status.

Usage:
    python scripts/inspect_checkpoint.py                        # List recent checkpoints
    python scripts/inspect_checkpoint.py <pipeline_run_id>      # Inspect specific pipeline
    python scripts/inspect_checkpoint.py <pipeline_run_id> --show-context
    python scripts/inspect_checkpoint.py <pipeline_run_id> --verify-recovery
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional
from elasticsearch import Elasticsearch

# Add parent directory to path to import orchestrator modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CheckpointInspector:
    """Inspects checkpoint files and recovery state"""

    def __init__(self, checkpoints_dir: str = "orchestrator_data/state/checkpoints", es_client: Optional[Elasticsearch] = None):
        self.checkpoints_dir = Path(checkpoints_dir)
        self.es_client = es_client

        # Create directory if it doesn't exist
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def find_checkpoint_files(self, pipeline_id: str) -> List[Path]:
        """Find all checkpoint files for a pipeline"""
        pattern = f"{pipeline_id}_stage_*.json"
        files = list(self.checkpoints_dir.glob(pattern))

        # Sort by stage number
        files.sort(key=lambda f: int(f.stem.split('_stage_')[1]))

        return files

    def read_checkpoint(self, checkpoint_file: Path) -> Optional[Dict[str, Any]]:
        """Read and parse a checkpoint file"""
        try:
            with open(checkpoint_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error reading {checkpoint_file}: {e}", file=sys.stderr)
            return None

    def get_latest_checkpoint(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent checkpoint for a pipeline"""
        files = self.find_checkpoint_files(pipeline_id)

        if not files:
            return None

        # Latest is the one with highest stage number
        latest_file = files[-1]
        return self.read_checkpoint(latest_file)

    def get_pipeline_context(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """Get pipeline run context from Elasticsearch"""
        if not self.es_client:
            return None

        try:
            query = {"query": {"term": {"id": pipeline_id}}}
            res = self.es_client.search(index="pipeline-runs-*", body=query)
            hits = res['hits']['hits']
            if hits:
                return hits[0]['_source']
        except Exception as e:
            print(f"Warning: Could not fetch pipeline context: {e}", file=sys.stderr)

        return None

    def list_recent_checkpoints(self, limit: int = 10) -> List[Dict[str, Any]]:
        """List recent checkpoints across all pipelines"""
        # Get all checkpoint files
        all_files = list(self.checkpoints_dir.glob("*_stage_*.json"))

        # Group by pipeline_id
        by_pipeline = {}
        for f in all_files:
            pipeline_id = '_'.join(f.stem.split('_')[:-2])  # Remove _stage_N
            if pipeline_id not in by_pipeline:
                by_pipeline[pipeline_id] = []
            by_pipeline[pipeline_id].append(f)

        # Get latest checkpoint for each pipeline
        recent = []
        for pipeline_id, files in by_pipeline.items():
            files.sort(key=lambda f: int(f.stem.split('_stage_')[1]))
            latest_file = files[-1]
            checkpoint = self.read_checkpoint(latest_file)

            if checkpoint:
                recent.append({
                    'pipeline_id': pipeline_id,
                    'stage_index': checkpoint['stage_index'],
                    'timestamp': checkpoint['timestamp'],
                    'file': str(latest_file),
                    'num_stages': len(files)
                })

        # Sort by timestamp, most recent first
        recent.sort(key=lambda x: x['timestamp'], reverse=True)

        return recent[:limit]

    def verify_recovery(self, checkpoint_data: Dict[str, Any]) -> Dict[str, Any]:
        """Verify that checkpoint can be used for recovery"""
        issues = []
        warnings = []

        # Check required fields
        required_fields = ['pipeline_id', 'stage_index', 'timestamp', 'context']
        for field in required_fields:
            if field not in checkpoint_data:
                issues.append(f"Missing required field: {field}")

        # Check context
        context = checkpoint_data.get('context', {})
        if not context:
            issues.append("Context is empty")
        else:
            # Check for important context fields
            context_fields = ['project', 'issue_number']
            for field in context_fields:
                if field not in context:
                    warnings.append(f"Missing context field: {field}")

        # Check if checkpoint is stale (> 24 hours)
        try:
            timestamp = datetime.fromisoformat(checkpoint_data['timestamp'])
            age = datetime.now() - timestamp
            if age > timedelta(hours=24):
                warnings.append(f"Checkpoint is stale ({self.format_duration(age)} old)")
        except Exception:
            warnings.append("Could not parse timestamp")

        # Verify JSON serializability
        try:
            json.dumps(checkpoint_data)
        except Exception as e:
            issues.append(f"Checkpoint is not JSON serializable: {e}")

        ready = len(issues) == 0

        return {
            'ready': ready,
            'issues': issues,
            'warnings': warnings
        }

    def format_duration(self, delta: timedelta) -> str:
        """Format timedelta as human-readable string"""
        total_seconds = int(delta.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0:
            return f"{hours} hour{'s' if hours != 1 else ''} {minutes} minute{'s' if minutes != 1 else ''}"
        elif minutes > 0:
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        else:
            return f"{total_seconds} second{'s' if total_seconds != 1 else ''}"

    def print_checkpoint_info(
        self,
        pipeline_id: str,
        checkpoint_files: List[Path],
        latest_checkpoint: Dict[str, Any],
        pipeline_context: Optional[Dict[str, Any]],
        show_context: bool = False,
        verify: bool = False
    ):
        """Print checkpoint information in human-readable format"""
        print(f"\nCheckpoint Inspection: {pipeline_id}")
        print("━" * 80)

        # Pipeline context from Elasticsearch
        if pipeline_context:
            print("\nPipeline Run Information:")
            print(f"  Issue: #{pipeline_context.get('issue_number')} - \"{pipeline_context.get('issue_title', 'N/A')}\"")
            print(f"  Project: {pipeline_context.get('project')}")
            print(f"  Board: {pipeline_context.get('board')}")
            print(f"  Started: {pipeline_context.get('started_at')}")
            print(f"  Status: {pipeline_context.get('status')}")
        else:
            print("\nPipeline Run Information:")
            print("  (Not found in Elasticsearch)")

        # Checkpoint files
        print(f"\nCheckpoint Files Found: {len(checkpoint_files)}")
        for f in checkpoint_files:
            stage_num = f.stem.split('_stage_')[1]
            print(f"  ✓ Stage {stage_num}: {f}")

        print("\n" + "━" * 80)

        # Latest checkpoint details
        if latest_checkpoint:
            print(f"\nLatest Checkpoint (Stage {latest_checkpoint['stage_index']}):")

            timestamp = datetime.fromisoformat(latest_checkpoint['timestamp'])
            age = datetime.now() - timestamp
            print(f"  Created: {latest_checkpoint['timestamp']}")
            print(f"  Age: {self.format_duration(age)}")

            # Context summary
            context = latest_checkpoint.get('context', {})
            print("\nContext Summary:")
            print(f"  Project: {context.get('project', 'N/A')}")
            print(f"  Issue: #{context.get('issue_number', 'N/A')}")
            print(f"  Board: {context.get('board', 'N/A')}")

            # Previous stage output length
            prev_output = context.get('previous_stage_output', '')
            if prev_output:
                print(f"  Previous Stage Output: {len(prev_output):,} characters")

            # Conversation history
            conv_history = context.get('conversation_history', [])
            if conv_history:
                print(f"  Conversation History: {len(conv_history)} turns")

            # Metrics
            metrics = context.get('metrics', {})
            if metrics:
                print(f"  Metrics: {json.dumps(metrics)}")

            # Show full context if requested
            if show_context:
                print("\n" + "━" * 80)
                print("\nFull Context:")
                print(json.dumps(context, indent=2))

            print("\n" + "━" * 80)

            # Stage progression
            print("\nStage Progression:")
            for i, f in enumerate(checkpoint_files):
                stage_num = int(f.stem.split('_stage_')[1])
                cp = self.read_checkpoint(f)
                if cp:
                    ts = cp.get('timestamp', 'unknown')
                    print(f"  [✓] Stage {stage_num} - Completed at {ts} (checkpoint saved)")

            # Show next stage as not reached
            next_stage = latest_checkpoint['stage_index'] + 1
            print(f"  [ ] Stage {next_stage} - Not reached")

            # Recovery verification
            if verify:
                print("\n" + "━" * 80)
                print("\nRecovery Verification:")
                verification = self.verify_recovery(latest_checkpoint)

                if verification['ready']:
                    print("  Status: ✓ READY")
                    print("  If pipeline crashes, will resume from Stage", latest_checkpoint['stage_index'])
                    print("  Context is serializable and complete")
                else:
                    print("  Status: ✗ NOT READY")
                    print("\n  Issues:")
                    for issue in verification['issues']:
                        print(f"    ✗ {issue}")

                if verification['warnings']:
                    print("\n  Warnings:")
                    for warning in verification['warnings']:
                        print(f"    ⚠ {warning}")

            print("\n" + "━" * 80)

            # Recommendations
            print("\nRecommendations:")
            verification = self.verify_recovery(latest_checkpoint)
            if verification['ready'] and not verification['warnings']:
                print("  ✓ Checkpoints are present and valid")
                print("  ✓ Latest checkpoint is recent")
                print("  ✓ Recovery context is complete")
            else:
                if verification['issues']:
                    print("  ✗ Checkpoint has issues - recovery may fail")
                if verification['warnings']:
                    for warning in verification['warnings']:
                        print(f"  ⚠ {warning}")

        print()

    def print_recent_checkpoints(self, recent: List[Dict[str, Any]]):
        """Print list of recent checkpoints"""
        print("\nRecent Checkpoints:")
        print("━" * 80)

        if not recent:
            print("\nNo checkpoints found.")
            print()
            return

        print(f"\nShowing {len(recent)} most recent checkpoints:\n")

        for cp in recent:
            pipeline_id = cp['pipeline_id']
            # Truncate long IDs for display
            if len(pipeline_id) > 40:
                display_id = pipeline_id[:37] + "..."
            else:
                display_id = pipeline_id

            timestamp = datetime.fromisoformat(cp['timestamp'])
            age = datetime.now() - timestamp

            print(f"Pipeline: {display_id}")
            print(f"  Latest Stage: {cp['stage_index']} (of {cp['num_stages']} checkpoints)")
            print(f"  Last Updated: {cp['timestamp']} ({self.format_duration(age)} ago)")
            print(f"  File: {cp['file']}")
            print()

        print("━" * 80)
        print("\nTo inspect a specific pipeline:")
        print("  python scripts/inspect_checkpoint.py <pipeline_run_id>")
        print()


def main():
    parser = argparse.ArgumentParser(description='Inspect pipeline checkpoints')
    parser.add_argument('pipeline_id', nargs='?',
                        help='Pipeline run ID to inspect (omit to list recent checkpoints)')
    parser.add_argument('--show-context', action='store_true',
                        help='Show full context JSON')
    parser.add_argument('--verify-recovery', action='store_true',
                        help='Verify checkpoint can be used for recovery')
    parser.add_argument('--checkpoints-dir', default='orchestrator_data/state/checkpoints',
                        help='Checkpoints directory (default: orchestrator_data/state/checkpoints)')
    parser.add_argument('--es-host', default='localhost',
                        help='Elasticsearch host (default: localhost)')
    parser.add_argument('--es-port', type=int, default=9200,
                        help='Elasticsearch port (default: 9200)')

    args = parser.parse_args()

    # Connect to Elasticsearch (optional)
    es_client = None
    try:
        es_client = Elasticsearch([f"http://{args.es_host}:{args.es_port}"])
        es_client.info()
    except Exception as e:
        print(f"Warning: Could not connect to Elasticsearch: {e}", file=sys.stderr)
        print("Pipeline context will not be available.", file=sys.stderr)

    # Create inspector
    inspector = CheckpointInspector(checkpoints_dir=args.checkpoints_dir, es_client=es_client)

    if args.pipeline_id:
        # Inspect specific pipeline
        checkpoint_files = inspector.find_checkpoint_files(args.pipeline_id)

        if not checkpoint_files:
            print(f"Error: No checkpoint files found for pipeline {args.pipeline_id}")
            print(f"Searched in: {inspector.checkpoints_dir}")
            sys.exit(1)

        latest_checkpoint = inspector.get_latest_checkpoint(args.pipeline_id)
        pipeline_context = inspector.get_pipeline_context(args.pipeline_id)

        inspector.print_checkpoint_info(
            args.pipeline_id,
            checkpoint_files,
            latest_checkpoint,
            pipeline_context,
            show_context=args.show_context,
            verify=args.verify_recovery
        )
    else:
        # List recent checkpoints
        recent = inspector.list_recent_checkpoints()
        inspector.print_recent_checkpoints(recent)


if __name__ == '__main__':
    main()
