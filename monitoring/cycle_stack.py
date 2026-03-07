"""
CycleStack — ordered hierarchy of execution cycle frames.

The stack travels as List[dict] (JSON-serializable) in context['cycle_stack']
so it passes through Redis and Elasticsearch boundaries without conversion.
"""

from dataclasses import dataclass, asdict
from typing import List, Optional


@dataclass
class CycleFrame:
    cycle_type: str      # e.g. "repair_cycle", "repair_test_type", "repair_fix",
                         #      "review_cycle", "pr_review_stage", "pr_review_phase"
    cycle_id: str        # unique ID for this instance
    iteration: int       # current iteration within this cycle (1-based)
    max_iterations: int  # 0 = no fixed limit
    label: str           # human-readable label


CycleStack = List[CycleFrame]


def push_frame(stack: List[dict], frame: CycleFrame) -> List[dict]:
    """Append a serialized frame to the stack (does not mutate)."""
    return list(stack) + [asdict(frame)]


def innermost(stack: List[dict]) -> Optional[dict]:
    return stack[-1] if stack else None


def cycle_type_from_stack(stack: List[dict]) -> str:
    f = innermost(stack)
    return f['cycle_type'] if f else ""


def cycle_iteration_from_stack(stack: List[dict]) -> int:
    f = innermost(stack)
    return f['iteration'] if f else 0
