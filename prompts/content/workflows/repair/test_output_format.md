---
invoked_by: pipeline/repair_cycle.py — concatenated onto every runner_*.md template
variables: none
notes: >
  Loaded as raw text and concatenated (not passed through str.format()).
  Literal { } in the JSON examples are safe because no .format() call is made on this file.
---

CRITICAL: You MUST return ONLY valid JSON in this EXACT format (no markdown, no explanation):
{
    "passed": <number of passing checks or tests>,
    "failed": <number of failing checks or tests>,
    "warnings": <number of items in warning_list (must equal len(warning_list), 0 if empty)>,
    "failures": [
        {"file": "<file path>", "test": "<check or test name>", "message": "<failure message>"},
        ...
    ],
    "warning_list": [
        {"file": "<file path>", "message": "<warning message>"},
        ...
    ]
}

If everything passes cleanly, return:
{"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}

DO NOT include any explanation, markdown formatting, or other text - ONLY the JSON object.
