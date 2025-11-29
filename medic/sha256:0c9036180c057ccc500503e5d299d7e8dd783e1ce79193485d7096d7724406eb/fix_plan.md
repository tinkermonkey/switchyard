# Fix Plan

**Failure Signature:** `sha256:0c9036180c057ccc500503e5d299d7e8dd783e1ce79193485d7096d7724406eb`

## Proposed Solution

Reorder the orchestrator startup sequence to perform GitHub project reconciliation **before** pipeline lock recovery. This ensures all GitHub state files exist before any code attempts to access them.

## Implementation Steps

1. Move the GitHub reconciliation loop (lines 379-414 in main.py) to occur **before** the pipeline lock recovery logic (lines 186-338)
2. Update the pipeline lock recovery to only process projects that have been successfully reconciled
3. Add defensive error handling in ProjectMonitor to gracefully skip projects without state instead of fatal exit

## Code Changes Required

### File: main.py

**Change 1: Move reconciliation before lock recovery**

```python
# Before (current order)
# Line 186-338: Pipeline lock recovery (uses temp_monitor)
# Line 379-414: GitHub reconciliation (creates state files)

# After (proposed order)
# Line 186-240: GitHub reconciliation (creates state files FIRST)
# Line 241-393: Pipeline lock recovery (can safely use state files)
```

**Specific change:**

Move this block from line 379-414:
```python
    # Reconcile all visible (non-hidden) projects on startup
    # Hidden projects (like test-project) are excluded from normal operations
    projects = config_manager.list_visible_projects()
    failure_count = 0
    for project_name in projects:
        # Always verify boards exist in GitHub, even if config hasn't changed
        # This handles the case where the orchestrator is moved to a new system
        needs_reconcile = github_state_manager.needs_reconciliation(project_name)

        if needs_reconcile:
            logger.info(f"Reconciling project configuration: {project_name} (config changed)")
        else:
            logger.info(f"Verifying project boards exist in GitHub: {project_name}")

        # Check GitHub circuit breaker before reconciliation
        from services.github_owner_utils import _github_circuit_breaker
        from services.circuit_breaker import CircuitState

        if _github_circuit_breaker.state == CircuitState.OPEN:
            logger.log_warning(
                f"Skipping reconciliation for {project_name} - GitHub circuit breaker is open "
                f"(will retry when circuit recovers)"
            )
            failure_count += 1
            continue

        # Always run reconciliation - it will discover existing boards if they exist
        success = await github_project_manager.reconcile_project(project_name)
        if not success:
            logger.log_error(f"Failed to reconcile project '{project_name}' - GitHub project management is not working")
            failure_count += 1

    # If all of the projects failed to reconcile, exit
    if failure_count == len(projects) and failure_count > 0:
        logger.log_error("All projects failed to reconcile - GitHub project management is not working")
        exit(1)
```

To appear **before** line 186 (before the "Recovering pipeline locks" section).

**Change 2: Add defensive check in lock recovery**

At line 199-230, before using temp_monitor, check if state exists:

```python
for project_name in config_manager.list_visible_projects():
    try:
        # Check if state exists before attempting lock recovery
        project_state = github_state_manager.load_project_state(project_name)
        if not project_state:
            logger.info(f"Skipping lock recovery for {project_name} - no GitHub state (not yet reconciled)")
            continue

        project_config = config_manager.get_project_config(project_name)
        for pipeline in project_config.pipelines:
            if not pipeline.active:
                continue
            # ... rest of lock recovery logic
    except Exception as e:
        logger.warning(f"Error during lock recovery for {project_name}: {e}")
        continue
```

### File: services/project_monitor.py

**Change 3: Make ProjectMonitor more resilient**

Replace the fatal exit at line 3526-3530 with graceful error handling:

```python
# Before
if not project_state:
    logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
    logger.error("This indicates GitHub project management failed during reconciliation")
    logger.error("Project monitoring cannot function without GitHub project state")
    logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
    exit(1)  # Fatal error - stop immediately

# After
if not project_state:
    logger.warning(f"No GitHub state found for project '{project_name}' - skipping monitoring")
    logger.warning("This may indicate the project is not yet reconciled or reconciliation failed")
    logger.warning(f"Project {project_name} will not be monitored until state is available")
    continue  # Skip this project and continue with others
```

This makes the monitor resilient to missing state while still logging the issue.

## Testing Strategy

### Manual Testing
1. Add a new project to `config/projects/`
2. Start the orchestrator
3. Verify it starts successfully without fatal errors
4. Verify the new project is reconciled during startup
5. Verify pipeline lock recovery works for existing projects

### Automated Testing
1. Create a unit test that:
   - Mocks a scenario with a project config but no state file
   - Verifies startup completes without exit(1)
   - Verifies reconciliation is attempted
2. Create an integration test that:
   - Adds a new project config
   - Starts the orchestrator
   - Verifies successful startup and reconciliation

### Verification
- Check logs show reconciliation happens before lock recovery
- Verify no FATAL errors appear for newly added projects
- Confirm existing projects' locks are still recovered correctly

## Risks and Considerations

**Risks:**
- Moving reconciliation earlier might delay other startup tasks
- Need to ensure GitHub circuit breaker state is properly handled early in startup

**Mitigations:**
- Reconciliation is async and typically fast (discovering existing boards)
- Circuit breaker check is already in place in the reconciliation code
- The change makes the system more deterministic by establishing required state early

**Backwards Compatibility:**
- No breaking changes to configuration or APIs
- Existing projects will reconcile normally
- Lock recovery behavior unchanged, just delayed until after reconciliation

## Deployment Plan

1. **Development:**
   - Make changes to main.py and project_monitor.py
   - Test locally with a new project configuration

2. **Testing:**
   - Run unit tests
   - Test startup with existing projects
   - Test startup with a newly added project
   - Verify lock recovery still works

3. **Deployment:**
   - Can be deployed immediately via Docker image rebuild
   - No database migrations needed
   - No configuration changes required
   - Safe to deploy - gracefully handles both old and new scenarios

4. **Rollback:**
   - If issues arise, revert the changes
   - No state corruption possible
   - Restart orchestrator to recover
