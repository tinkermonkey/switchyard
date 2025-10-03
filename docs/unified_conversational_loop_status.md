# Unified Conversational Loop - Implementation Status

## What We Built

### Architecture
Replaced the old separate feedback manager with a unified conversational loop system that handles both:
- **Conversational mode**: Human feedback loops for Research/Analysis columns
- **Review mode**: Automated maker-checker loops with human escalation for Review columns

### Key Files

**New:**
- `services/conversational_loop.py` - Unified loop executor for both modes

**Modified:**
- `config/foundations/workflows.yaml` - Added `type: conversational` to Research/Analysis columns
- `services/project_monitor.py` - Routes to conversational loop, disabled old feedback manager
- `claude/docker_runner.py` - Attempted fix for large prompts via stdin (still has issues)

**Disabled:**
- Old feedback manager in `project_monitor.py` (methods `check_for_feedback` and `check_for_feedback_in_discussion`)

## Current Status

### ✅ Working
1. Conversational loop starts correctly when card moved to Analysis/Research
2. Agent executes and posts to discussion
3. Loop enters monitoring mode
4. No interference from old feedback manager

### ⚠️ Needs Debugging
1. **Feedback detection not working** - `_get_human_feedback_since_last_agent()` appears to run but doesn't detect comments
2. Added error logging to catch issues (line 161-170 in conversational_loop.py)
3. Need to restart and check logs for errors

### 🔴 Known Issues
1. **Large prompt handling** - Review cycles fail with "Argument list too long" when discussion context is large
   - Attempted stdin approach but has broken pipe issues
   - May need file mounting or context size reduction instead

## Next Steps

### Immediate (Debugging Feedback Detection)
1. Restart orchestrator with new error logging
2. Move issue to Analysis to trigger conversational loop
3. Post comment on discussion
4. Check logs for error in `_get_human_feedback_since_last_agent()`
5. Likely issues:
   - GraphQL query format mismatch
   - Timestamp comparison logic (timezone issues?)
   - Reply structure not matching expected format

### Short Term
1. Fix feedback detection
2. Test full conversational flow (agent → human feedback → agent response)
3. Verify timeout and iteration handling

### Medium Term
1. Fix large prompt issue for review cycles
   - Consider: reduce context size passed to agents
   - Or: mount temp file in Docker instead of stdin
2. Integrate review cycle into conversational loop (currently separate)
3. Test escalation flow (review → blocked → human feedback → reviewer update)

## Testing Instructions

### Test Conversational Mode
```bash
# 1. Move issue to Analysis column
# 2. Watch logs
docker-compose logs orchestrator -f | grep conversational

# 3. Verify agent posts to discussion
# 4. Post comment on discussion (no @mention needed)
# 5. Wait 30 seconds, check logs for "Human feedback detected"
```

### Current Logging Points
- `Starting conversational loop` - Loop initiated
- `Monitoring discussion X for human feedback` - Started monitoring
- `Still monitoring for feedback (X/3600s)` - Every 5 minutes
- `Checking X comments for feedback` - Feedback detection running
- `Found human feedback from X` - Comment detected
- `Error checking for feedback` - Detection error (NEW)

## Configuration Reference

### Conversational Column
```yaml
- name: "Analysis"
  type: "conversational"
  agent: "business_analyst"
  feedback_timeout_seconds: 3600  # 1 hour
```

### Review Column
```yaml
- name: "Review"
  type: "review"
  agent: "requirements_reviewer"
  maker_agent: "business_analyst"
  max_iterations: 3
  escalate_on_blocked: true
```

## Code Locations

### Feedback Detection
- `services/conversational_loop.py:282-383` - `_get_human_feedback_since_last_agent()`
- Line 338 - Logs number of comments being checked
- Line 358 - Logs number of replies per comment
- Line 368 - Logs each reply with timestamp
- Line 372 - Returns when human feedback found

### Monitoring Loop
- `services/conversational_loop.py:133-202` - `_conversational_loop()`
- Line 157 - 30 second poll interval
- Line 186 - 5 minute progress logging

### Agent Execution
- `services/conversational_loop.py:221-280` - `_execute_agent()`
- Line 264-270 - Creates agent and executes directly

## Troubleshooting

### Loop not starting
- Check `docker-compose logs orchestrator | grep "Starting conversational"`
- Verify column has `type: conversational` in workflows.yaml
- Check project_monitor routes to `_start_conversational_loop_for_issue`

### Agent not executing
- Check for `PipelineFactory` errors
- Verify agent exists in foundations/agents.yaml
- Check circuit breaker status

### Feedback not detected
- Enable info logging in conversational_loop.py (already done)
- Check "Checking X comments" appears in logs
- Verify GraphQL query returns data
- Check timestamp comparison (timezone issues)

### Loop exits early
- Check for exceptions in logs
- Verify asyncio.sleep doesn't fail
- Check state.discussion_id is valid
