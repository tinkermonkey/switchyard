# Investigation Outcome: Ignored

**Failure Signature:** `sha256:480fd975bbc9081df8b679f409e64dcdd0764fd13df40e47e18652ba5732d0bb`
**Investigation Date:** 2025-11-29

## Reason for Ignoring

This is **expected behavior** from APScheduler, not an actionable bug. The warning occurs when:

1. The `cleanup_orphaned_containers` job is scheduled to run every 15 minutes (at :00, :15, :30, :45)
2. Another long-running scheduled job (e.g., "Cleanup orphaned feature branches" at 02:00, "Check for stale feature branches" at 09:00) starts executing at exactly the same time
3. The long-running job takes 2-3 seconds to complete
4. APScheduler detects that the :00 slot for the container cleanup job was "missed" by 2-3 seconds

**Evidence from logs:**
```
2025-11-29 02:00:00,009 - Running job "Cleanup orphaned feature branches" (scheduled at 2025-11-29 02:00:00)
[... job executes for 2.5 seconds ...]
2025-11-29 02:00:02,542 - Job "Cleanup orphaned feature branches" executed successfully
2025-11-29 02:00:02,543 - WARNING - Run time of job "Cleanup orphaned agent container tracking keys" was missed by 0:00:02.543007
```

The container cleanup job ran successfully at 01:45:00 and will run again at 02:15:00. The "missed" :00 execution is harmless because:
- The job will run at the next scheduled time (15 minutes later)
- Missing a single cleanup cycle has no impact (cleanup happens 96 times per day)
- The cleanup is idempotent and best-effort

## Severity Assessment

- **Severity:** Informational
- **Impact:** None - cleanup continues on schedule
- **Frequency:** 2 times in 24 hours (only when hourly jobs coincide with :00 or :15 slots)

## Recommendation

**Option 1 (Preferred): Suppress the warning**
Configure APScheduler with `misfire_grace_time=60` for the container cleanup job. This tells APScheduler that missing the execution by up to 60 seconds is acceptable.

**Option 2: Adjust schedule**
Change the container cleanup to run at offsets that don't conflict (e.g., every 15 minutes starting at :03).

**Option 3: Do nothing**
This warning is cosmetic and doesn't indicate any malfunction. The Medic system can ignore this fingerprint with a filter rule for APScheduler misfire warnings < 10 seconds.

## Filter Rule Suggestion

Since this is expected behavior and not actionable, create a Medic filter to ignore APScheduler misfire warnings where the delay is less than 10 seconds:

```yaml
pattern: 'Run time of job .* was missed by 0:00:0[0-9]\.'
severity: WARNING
logger: apscheduler.executors.default
action: ignore
reason: APScheduler misfire warnings under 10 seconds are expected when jobs overlap
```
