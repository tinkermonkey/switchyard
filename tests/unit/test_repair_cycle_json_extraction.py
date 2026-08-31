"""
Unit tests for RepairCycleStage._extract_json_from_response.

Covers narrative-wrapped JSON responses — the dominant real-world failure mode
where a maker agent returns its test result JSON alongside prose commentary
instead of the bare JSON object the repair cycle requires. See pipeline run
a507e510-c62b-4a93-af86-2f78ff3c24da and 5a4d12e7-72d2-4b11-84e2-263952488037
for the incidents that motivated this.
"""

import json

import pytest

from pipeline.repair_cycle import RepairCycleStage


def extract(response: str):
    # _extract_json_from_response doesn't touch `self`, so an unbound call is fine.
    return RepairCycleStage._extract_json_from_response(object(), response)


class TestExtractJsonFromResponse:
    def test_pure_json_still_parses_directly(self):
        response = json.dumps({"passed": 5, "failed": 0, "failures": [], "warning_list": []})
        result = extract(response)
        assert result == {"passed": 5, "failed": 0, "failures": [], "warning_list": []}

    def test_json_in_markdown_fence_still_parses(self):
        response = (
            "Here you go:\n```json\n"
            + json.dumps({"passed": 3, "failed": 1, "failures": [], "warning_list": []})
            + "\n```"
        )
        result = extract(response)
        assert result["passed"] == 3 and result["failed"] == 1

    def test_narrative_before_json_is_extracted(self):
        """The literal shape from a507e510's second integration-test attempt:
        prose, then the real JSON, with no code fence."""
        payload = {
            "passed": 0,
            "failed": 1,
            "warnings": 0,
            "failures": [{
                "file": "__infrastructure__",
                "test": "pytest_execution_failed",
                "message": "ModuleNotFoundError: No module named 'utils' — collection failed with 41 errors",
            }],
            "warning_list": [],
        }
        response = "I ran the integration suite; here are the results:\n" + json.dumps(payload)
        result = extract(response)
        assert result["failed"] == 1
        assert "ModuleNotFoundError" in result["failures"][0]["message"]

    def test_narrative_after_json_is_extracted(self):
        payload = {"passed": 892, "failed": 0, "failures": [], "warning_list": []}
        response = json.dumps(payload) + "\nAll integration tests passed, great work!"
        result = extract(response)
        assert result["passed"] == 892

    def test_braces_inside_failure_message_do_not_break_extraction(self):
        """A failure/error message that itself quotes a dict or JSON snippet
        (e.g. a Python repr or an API error body) contains literal '{'/'}'
        characters. The old brace-counting regex loses its balance on these
        and fails to extract anything; the decoder-based scan must not."""
        payload = {
            "passed": 10,
            "failed": 1,
            "warnings": 0,
            "failures": [{
                "file": "adapters/api_client.py",
                "test": "test_error_response_handling",
                "message": "Unhandled error body: {'code': 500, 'detail': {'reason': 'timeout'}}",
            }],
            "warning_list": [],
        }
        response = "Test run complete.\n" + json.dumps(payload) + "\nSee above for details."
        result = extract(response)
        assert result["failed"] == 1
        assert result["failures"][0]["message"].startswith("Unhandled error body:")

    def test_last_matching_candidate_wins_when_multiple_present(self):
        """If the response contains more than one schema-matching object
        (e.g. the agent echoes an example format before the real result),
        the final one is treated as authoritative."""
        example = {"passed": 0, "failed": 0, "failures": [], "warning_list": []}
        real = {"passed": 7, "failed": 2, "failures": [], "warning_list": []}
        response = (
            f"Expected format example: {json.dumps(example)}\n"
            f"Actual result: {json.dumps(real)}"
        )
        result = extract(response)
        assert result == real

    def test_pure_narrative_with_no_json_still_raises(self):
        """The genuinely unrecoverable case from both incidents: no JSON
        object anywhere in the response. Must still raise, not fabricate
        a result."""
        response = (
            "I'm monitoring the integration test execution. Let me wait for it "
            "to complete and then parse the results."
        )
        with pytest.raises(ValueError, match="No valid JSON test result found"):
            extract(response)

    def test_empty_response_raises(self):
        with pytest.raises(ValueError, match="Empty response"):
            extract("")

    def test_json_missing_required_fields_is_not_treated_as_match(self):
        """An unrelated object elsewhere in the narrative (missing passed/failed)
        must be skipped in favor of the real result, not accidentally returned."""
        unrelated = {"note": "this is not a test result"}
        real = {"passed": 1, "failed": 0, "failures": [], "warning_list": []}
        response = f"Context: {json.dumps(unrelated)}\nResult: {json.dumps(real)}"
        result = extract(response)
        assert result == real
