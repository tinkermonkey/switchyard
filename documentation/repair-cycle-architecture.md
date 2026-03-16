# Repair cycle architecture

## What a repair cycle is

A repair cycle is a deterministic test-fix-validate loop that runs automatically when an issue reaches a pipeline stage configured with `stage_type: repair_cycle`. It solves the problem of agent-produced code that passes review but fails automated quality checks: instead of returning the issue to a developer or moving it backwards through the pipeline, the orchestrator autonomously iterates — running tests, identifying failures, dispatching fix agents, re-running tests — until all tests pass or a configured limit is reached.

The loop is not a maker-checker pattern. It has explicit convergence criteria (all tests pass) and a deterministic iteration structure. Each "test type" (e.g., `unit`, `integration`, `ci`, `compilation`, `pre-commit`, `storybook`) is processed sequentially. Failure in any test type stops processing of subsequent types.

The repair cycle runs entirely inside a dedicated Docker container that is separate from the project's agent containers. This isolation means the orchestrator can restart without interrupting an in-progress repair cycle.

---

## How the orchestrator detects that a repair cycle is needed

`ProjectMonitor` polls GitHub project boards on a 30-second interval. When it processes an issue that is in a column, it resolves the pipeline stage configuration for that column. If `stage_config.stage_type == 'repair_cycle'` (checked at line 2515 of `services/project_monitor.py`), the normal agent dispatch path is bypassed and `_start_repair_cycle_for_issue()` is called instead.

Test type configurations come from the project config file under `project.testing.types`. Each entry specifies the test type, iteration limits, and thresholds. If no `testing.types` entries exist, the repair cycle is skipped with a warning and no container is launched.

---

## Repair cycle execution flow

### 1. Pre-flight checks

Before launching anything, `_start_repair_cycle_for_issue()` performs two checks:

- **Duplicate container check**: Queries Redis for `repair_cycle:container:{project}:{issue}` and then verifies with `docker ps` that the container is actually running. If a container is already running for this issue, the launch is skipped. If the Redis key is stale (container not found in Docker), the key is deleted and the launch proceeds.
- **Competing repair cycle check**: Reads the pipeline lock. If another issue holds the lock and that issue also has a repair cycle container registered in Redis, the launch is skipped to prevent two repair cycles from competing for the same workspace.

### 2. Pipeline lock acquisition

Repair cycles take priority over normal pipeline stages. If another issue holds the pipeline lock for a different reason (e.g., it is in Development), the lock is stolen: the old lock is released and a new lock is created for the repair cycle issue. If another repair cycle holds the lock, the new cycle yields and does not start. On success, the lock is released when the issue is auto-advanced. On failure, the lock is intentionally retained — see [Limits and safeguards](#limits-and-safeguards).

### 3. Context serialization

`_save_repair_cycle_context()` serializes the full execution context — project name, issue number, pipeline run ID, test configurations, agent name, workspace type, branch name, observability handles — to a JSON file at:

```
/workspace/switchyard/orchestrator_data/repair_cycles/{project}/{issue_number}/context.json
```

This file is the sole input to the container. If the container is restarted, it reloads from this file.

### 4. Container launch

`_launch_repair_cycle_container()` runs the container with:

```
docker run --rm --name repair-cycle-{project}-{issue}-{run_id[:8]} \
  --network {orchestrator_network} \
  --detach \
  --user 1000 \
  -v {host_workspace}/switchyard:/app \
  -v {host_workspace}/switchyard:/workspace/switchyard \
  -v {host_workspace}/{project}:/workspace/{project} \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ...environment variables... \
  switchyard-orchestrator \
  python -m pipeline.repair_cycle_runner \
    --project {project} \
    --issue {issue_number} \
    --pipeline-run-id {run_id} \
    --stage {stage_name} \
    --context /workspace/switchyard/orchestrator_data/repair_cycles/{project}/{issue_number}/context.json
```

The image is `switchyard-orchestrator` — the same image that runs the orchestrator itself. This gives the container access to the repair cycle runner code without a separate build step. See [The Docker container used](#the-docker-container-used) for details.

After the container starts, its name is stored in Redis at `repair_cycle:container:{project}:{issue}` with a 2-hour TTL, and the full pipeline run ID is stored at `repair_cycle:full_run_id:{container_name}` with the same TTL.

The work execution state is recorded via `work_execution_tracker.record_execution_start()` before the container launches to prevent race conditions.

### 5. Inner execution loop

Inside the container, `RepairCycleRunner.run()` in `pipeline/repair_cycle_runner.py`:

1. Loads context from the JSON file.
2. Constructs a `RepairCycleStage` instance (from `pipeline/repair_cycle.py`) with the test configurations from the context.
3. Checks for an existing checkpoint (see [Checkpointing](#checkpointing)).
4. Calls `RepairCycleStage.execute()`.

`RepairCycleStage.execute()` iterates over each `RepairTestRunConfig`. For each test type, `_run_test_cycle()` executes the following loop up to `max_iterations` times:

1. **Run tests** — calls `_run_tests()`, which dispatches the configured agent via `AgentExecutor.execute_agent()` with `execution_type="repair_test"`. The agent receives a `direct_prompt` appropriate to the test type and must return a JSON object matching the schema: `{"passed": int, "failed": int, "warnings": int, "failures": [...], "warning_list": [...]}`.
2. **Check for infrastructure failures** — if the agent returns a `RepairTestFailure` with `file == "__infrastructure__"`, the cycle terminates immediately. This sentinel indicates the test runner itself failed (JSON parse failure after retries, container error, etc.).
3. **Check for systemic failures** (first time failures are encountered per test type) — calls `_analyze_systemic_failures()`, which dispatches a single agent call to classify the failures as environmental (require Docker image rebuild) or systemic code issues (same bug pattern across many files). The guard is `test_type not in _systemic_analysis_done_for`; once run for a given test type, it is skipped on all subsequent iterations for that test type even if failures persist. If environmental issues are found, `_run_env_rebuild_sub_cycle()` runs. If systemic code issues are found, `_run_systemic_fix_sub_cycle()` runs.
4. **Threshold systemic fix** — on any iteration where `test_result.failed >= systemic_analysis_threshold` (default 6) and a systemic fix has not already run this iteration, `_run_systemic_fix_sub_cycle()` is invoked with a broad diagnostic prompt rather than grinding through per-file fixes.
5. **Per-file fix** — `_fix_failures_by_file()` groups failures by test file and dispatches one agent call per file, with a prompt describing the specific failures in that file. The agent is instructed to identify root causes, remove obsolete tests, and run formatters rather than hand-editing style.
6. **Warning review** (if `review_warnings=True`) — after all failures are resolved, `_handle_warnings()` dispatches one agent call per source file with warnings, instructing the agent to determine if each warning is expected and fix it if not. If warning fixes introduce new test failures, the loop continues.
7. **Checkpoint** — `_checkpoint()` is called after each test run and after each fix pass.

If the outer loop exhausts `max_iterations` without all tests passing, a final test run is executed. If that run passes (e.g., the last fix attempt worked), the result is success regardless of iteration count.

### 6. Result persistence and exit

On completion, `RepairCycleRunner.save_result_to_redis()` writes the result dict to Redis at:

```
repair_cycle:result:{project}:{issue}:{run_id}
```

with a 24-hour TTL. The container then exits with one of these codes:

| Code | Meaning |
|------|---------|
| 0 | All tests passed |
| 1 | Max iterations reached or tests still failing |
| 2 | Execution error, agent failure, or failed to save result to Redis |
| 3 | Timeout |
| 4 | Cancelled (SIGTERM, SIGINT, or `CancellationError`) |

### 7. Container monitoring and post-processing

Back in the orchestrator, `_monitor_repair_cycle_container()` runs in a background thread using `subprocess.Popen(['docker', 'wait', container_name])`. It checks for container progress every hour. If no observability events have been emitted for a full hour (detected via `_is_repair_cycle_stalled()`), the container is killed and the cycle is marked failed.

When the container exits, the monitor:

1. Loads the result from Redis via `_load_repair_cycle_result_from_redis()`.
2. Posts a summary comment to the GitHub issue.
3. If successful: calls `auto_commit_service.commit_agent_changes()`, then uses `PipelineProgression.move_issue_to_column()` to advance the issue to the next column, then calls `pipeline_run_manager.end_pipeline_run()`.
4. If the next column is an exit column (`Staged` or `Done`), calls `_check_pr_ready_on_issue_exit()`.
5. If failed: posts a failure summary via `_post_repair_cycle_failure_summary()`, applies the `repair-cycle:failed` label to the GitHub issue, ends the pipeline run with `retain_lock=True`, and sets a Redis marker at `pipeline_lock:repair_failed:{project}:{board}:{issue}` so the stale-lock watchdog does not treat the retained lock as leaked.
6. Always removes the container and clears the Redis tracking key.

---

## The repair cycle runner

`RepairCycleRunner` in `pipeline/repair_cycle_runner.py` is the entry point for the container process. It coordinates the execution sequence:

1. **Context loading** (`load_context()`) — reads the JSON context file serialized before launch, or constructs a minimal context from CLI arguments if the file is absent.
2. **Stage initialization** (`initialize_stage()`) — constructs `RepairCycleCheckpoint` and `RepairCycleStage` from the loaded context. Validates that at least one `RepairTestRunConfig` exists.
3. **Checkpoint check** — compares the checkpoint's `pipeline_run_id` against the current run ID. If they match, restores `_agent_call_count` so the circuit breaker continues from the correct count. If they differ (stale checkpoint from a previous run), clears the checkpoint and starts fresh.
4. **Execution** (`execute_repair_cycle()`) — calls `RepairCycleStage.execute()`. Handles `CancellationError` and `asyncio.CancelledError` cleanly.
5. **Result save** (`save_result_to_redis()`) — writes the result to Redis. Returns exit code 2 if this fails, because the monitoring thread cannot recover without the result.

Signal handlers for `SIGTERM` and `SIGINT` set `self._cancelled = True`, which causes the run method to return exit code 4 after any in-progress agent call completes.

Logs are written to both stdout and `/workspace/{project}/.repair_cycle.log`.

### Context provided to the repair agent

Each agent call (test run, fix, warning review, systemic analysis) receives the following context:

- Full project context: project name, issue number, board, repository, pipeline run ID.
- `direct_prompt`: a type-specific prompt defining exactly what to run and what output format to produce.
- `skip_workspace_prep: True`: prevents the agent from re-initializing the workspace on each call.
- `review_cycle: None`: prevents iteration count confusion from any prior review cycle state.
- `cycle_stack`: a stack of `CycleFrame` objects that describe the nesting level for observability (repair cycle → test type → iteration or fix operation).

---

## Checkpointing

### What is saved

`RepairCycleCheckpoint` in `pipeline/repair_cycle_checkpoint.py` saves state using the `create_checkpoint_state()` helper, which produces a dict with:

- `version`: schema version (`"1.0"`)
- `project`, `issue_number`, `pipeline_run_id`, `stage_name`
- `test_type`: the test type currently being processed
- `test_type_index`: 1-based position in the `test_configs` list (uses `enumerate(test_configs, start=1)`)
- `iteration`: iteration count within the current test type
- `agent_call_count`: total agent calls made (used to restore the circuit breaker)
- `files_fixed`, `test_results`, `cycle_results`

### Where checkpoints are stored

Checkpoints are stored outside the project workspace to avoid polluting the managed git repository:

```
/workspace/switchyard/state/projects/{project}/repair_cycles/{issue_number}/checkpoint.json
/workspace/switchyard/state/projects/{project}/repair_cycles/{issue_number}/checkpoint.backup.json
```

### How writes are atomic

`save_checkpoint()` writes to a `.tmp` file first, then uses `Path.replace()` (an atomic rename on POSIX filesystems) to move it to `checkpoint.json`. The previous `checkpoint.json` is copied to `checkpoint.backup.json` before the rename, providing a fallback if the primary checkpoint is corrupted.

### How recovery works after a restart

On startup, `execute_repair_cycle()` calls `load_checkpoint()`, which tries the primary file first and falls back to the backup. A loaded checkpoint is only used if its `pipeline_run_id` matches the current run's ID. If the IDs differ, the checkpoint is from a previous pipeline run and is discarded.

If a matching checkpoint is found, `_agent_call_count` is restored so the circuit breaker picks up from the correct value. The `RepairCycleStage.execute()` method then restarts iteration from the beginning, but because the agent call count is preserved, the circuit breaker budget is not reset.

Checkpoints are cleared on successful completion via `checkpoint_manager.clear_checkpoint()`. They are not cleared on failure, allowing inspection after the fact.

---

## Relationship between repair cycles and the main pipeline

The repair cycle does not pause the pipeline in the traditional sense — it runs in a detached Docker container and the orchestrator's monitoring loop continues polling all boards normally.

The pipeline lock is the coordination mechanism. While the repair cycle container runs, the lock is held by the repair cycle's issue. The `ProjectMonitor` polling loop skips dispatching new work for any issue that already holds its own lock, and will not start work for other issues on that pipeline either (one concurrent execution per pipeline). This effectively serializes the pipeline while the repair cycle runs.

When the repair cycle succeeds, the issue is moved to the next column by `PipelineProgression.move_issue_to_column()`, which triggers the lock release. Normal pipeline execution resumes when the next board poll detects the issue in its new column.

When the repair cycle fails, the lock is retained (`retain_lock=True` in `end_pipeline_run()`). The issue stays in the Testing column, holding the pipeline lock, until a human manually moves the issue. The stale-lock watchdog recognizes the `pipeline_lock:repair_failed:{project}:{board}:{issue}` Redis marker and does not treat this as a leaked lock.

---

## Limits and safeguards

### Per-test-type iteration limit

`RepairTestRunConfig.max_iterations` (default 5) caps the number of test-fix-validate loops for a single test type. On hitting the limit, a final test run is performed. If that run passes, the result is still counted as success.

### Per-file iteration limit

`RepairTestRunConfig.max_file_iterations` (default 3) is passed as metadata in the cycle stack and constrains how many times the same file can be targeted per iteration. The value flows through to the `CycleFrame` label but is not enforced as a hard loop boundary within `_fix_failures_by_file()` — each call processes all failing files once per outer iteration.

### Total agent call circuit breaker

`RepairCycleStage.max_total_agent_calls` (default 100, configurable in the stage config under `max_total_agent_calls`) is a hard cap across all agent calls for the entire repair cycle — test runs, fixes, warning reviews, and systemic analysis all count against the same counter. When `_agent_call_count >= max_total_agent_calls`, any new iteration returns a `CycleResult` with `error="Circuit breaker: max agent calls reached"` and emits `EventType.CIRCUIT_BREAKER_OPENED`. Systemic analysis also checks this limit before dispatching.

### Stall detection

The monitoring thread in `_monitor_repair_cycle_container()` wakes every hour to check progress. If `_is_repair_cycle_stalled()` finds no observability events for the `pipeline_run_id` in the past hour (queried from Elasticsearch), the container is killed with `docker kill` and the cycle is marked failed.

### Competing repair cycle prevention

At launch time, `_start_repair_cycle_for_issue()` checks whether the pipeline lock is held by another issue with an active repair cycle container in Redis. If so, the new repair cycle yields and does not start. Only one repair cycle runs per pipeline board at a time.

### Failure escalation

When the repair cycle fails, the issue is left in the Testing column with the pipeline lock retained and the `repair-cycle:failed` label applied to the GitHub issue. A summary comment with suggested next steps is posted. No automated retry is triggered. The next cycle for the same issue only starts after a human moves the issue, which clears the `repair_failed` Redis marker.

### Fast-fail across test types

If any test type fails, `RepairCycleStage.execute()` breaks out of the test-type loop immediately. Integration tests do not run if unit tests fail.

---

## The Docker container used

Repair cycle containers differ from normal agent containers in several important ways.

**Image**: Repair cycle containers use the `switchyard-orchestrator` image — the orchestrator's own image — not the project's `Dockerfile.agent` image. This means the container has access to the Python codebase (including `pipeline/repair_cycle_runner.py`), Redis client libraries, and the orchestrator's monitoring infrastructure. The project's agent image is used only for the sub-containers that the repair cycle spawns internally.

**Entry point**: The container runs `python -m pipeline.repair_cycle_runner` rather than `claude`. It is a Python process, not a Claude CLI session.

**Container name format**: `repair-cycle-{project}-{issue}-{run_id[:8]}`, compared to `claude-agent-{project}-{task_id}` for normal agents. The `AgentContainerRecovery` service skips repair cycle containers during normal agent recovery (checked via the `org.switchyard.execution_type` label, falling back to detecting `repair_` in the container name).

**Volume mounts**: The container receives:
- `/workspace/switchyard` (both at `/app` and `/workspace/switchyard`) — for the orchestrator code and checkpoint state paths
- `/workspace/{project}` — for the project files the agents will read and modify
- `/var/run/docker.sock` — so the repair cycle can spawn inner agent containers via Docker-in-Docker

**Lifetime**: The container is launched with `--rm` (auto-removed on exit) and `--detach` (runs in background). It is not connected to the orchestrator's asyncio event loop. It can outlive an orchestrator restart.

**Logging**: Logs go to stdout (captured by Docker) and to `/workspace/{project}/.repair_cycle.log`. The log file persists after the container exits and is not automatically cleaned up.

**Inner agent containers**: When `RepairCycleStage` calls `AgentExecutor.execute_agent()`, that call goes through the same `DockerAgentRunner` path as normal agents. These inner containers use the project's `Dockerfile.agent` image, mount the project workspace, and run Claude CLI. Their names follow the `claude-agent-{project}-{task_id}` pattern. The `_kill_child_agent_containers()` function, called when the repair cycle container is killed for staleness, terminates any inner containers associated with the same pipeline run ID.
