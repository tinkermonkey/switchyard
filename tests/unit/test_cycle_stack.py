"""Unit tests for monitoring.cycle_stack"""

import pytest
from monitoring.cycle_stack import (
    CycleFrame,
    push_frame,
    innermost,
    cycle_type_from_stack,
    cycle_iteration_from_stack,
)


def _make_frame(**kwargs) -> CycleFrame:
    defaults = dict(cycle_type="repair_cycle", cycle_id="id1", iteration=1, max_iterations=5, label="lbl")
    defaults.update(kwargs)
    return CycleFrame(**defaults)


class TestPushFrame:
    def test_empty_stack_returns_single_element(self):
        frame = _make_frame(cycle_type="repair_cycle", iteration=1)
        result = push_frame([], frame)
        assert len(result) == 1
        assert result[0]['cycle_type'] == "repair_cycle"
        assert result[0]['iteration'] == 1

    def test_push_does_not_mutate_original(self):
        parent = [{'cycle_type': 'repair_cycle', 'cycle_id': 'x', 'iteration': 1, 'max_iterations': 1, 'label': 'l'}]
        child = push_frame(parent, _make_frame(cycle_type="repair_test_type"))
        assert len(parent) == 1  # original unchanged
        assert len(child) == 2

    def test_nested_push_builds_hierarchy(self):
        s0 = []
        s1 = push_frame(s0, _make_frame(cycle_type="repair_cycle", iteration=1))
        s2 = push_frame(s1, _make_frame(cycle_type="repair_test_type", iteration=1))
        s3 = push_frame(s2, _make_frame(cycle_type="repair_test_iteration", iteration=3))
        assert len(s3) == 3
        assert s3[0]['cycle_type'] == "repair_cycle"
        assert s3[1]['cycle_type'] == "repair_test_type"
        assert s3[2]['cycle_type'] == "repair_test_iteration"

    def test_serialised_as_plain_dicts(self):
        result = push_frame([], _make_frame())
        assert isinstance(result[0], dict)
        import json
        json.dumps(result)  # must be JSON-serializable


class TestInnermost:
    def test_empty_stack_returns_none(self):
        assert innermost([]) is None

    def test_single_element(self):
        frame_dict = {'cycle_type': 'review_cycle', 'cycle_id': 'c', 'iteration': 2, 'max_iterations': 3, 'label': 'l'}
        assert innermost([frame_dict]) == frame_dict

    def test_returns_last_element(self):
        a = {'cycle_type': 'a', 'cycle_id': '', 'iteration': 1, 'max_iterations': 1, 'label': ''}
        b = {'cycle_type': 'b', 'cycle_id': '', 'iteration': 2, 'max_iterations': 2, 'label': ''}
        assert innermost([a, b]) == b


class TestCycleTypeFromStack:
    def test_empty_returns_empty_string(self):
        assert cycle_type_from_stack([]) == ""

    def test_returns_innermost_cycle_type(self):
        stack = push_frame(
            push_frame([], _make_frame(cycle_type="repair_cycle", iteration=1)),
            _make_frame(cycle_type="repair_fix", iteration=1),
        )
        assert cycle_type_from_stack(stack) == "repair_fix"


class TestCycleIterationFromStack:
    def test_empty_returns_zero(self):
        assert cycle_iteration_from_stack([]) == 0

    def test_returns_innermost_iteration(self):
        stack = push_frame(
            push_frame([], _make_frame(cycle_type="repair_cycle", iteration=1)),
            _make_frame(cycle_type="repair_test_iteration", iteration=3),
        )
        assert cycle_iteration_from_stack(stack) == 3
