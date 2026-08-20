---
invoked_by: pipeline/repair_cycle.py — concatenated onto every runner_*.md template
variables: none
notes: >
  Loaded as raw text and concatenated (not passed through str.format()).
  Literal { } in the JSON examples are safe because no .format() call is made on this file.
---

CRITICAL: You MUST return ONLY valid JSON in this EXACT format (no markdown, no explanation):
```
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
```

If everything passes cleanly, return:
{"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}

DO NOT include any explanation, markdown formatting, or other text - ONLY the JSON object.

CRITICAL — DO NOT DEFER TO THE BACKGROUND: If a test command is still executing, keep waiting
on that same tool call — do not end your turn until it has actually returned. Never respond with
a status update such as "tests are running in the background", "I'll report results once they
complete", "waiting for the process to finish", or "will check back shortly". There is no later
turn in which you will be asked to check on it — if you end your turn without the JSON object
above, this task is treated as a total failure, even if the tests you started actually passed.
This holds no matter how long the command takes to finish.
