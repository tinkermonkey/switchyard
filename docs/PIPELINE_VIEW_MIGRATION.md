# Pipeline View Migration

## Overview

The original pipeline view has been replaced with a new comprehensive Pipeline Run visualization that provides end-to-end traceability of issues through the workflow.

## What Changed

### Removed Files
- `web_ui/src/routes/pipeline.jsx` - Old pipeline view

### New Files
- `web_ui/src/routes/pipeline-run.jsx` - New pipeline run visualization
- `docs/PIPELINE_RUN_VISUALIZATION.md` - Comprehensive documentation

### Modified Files
- `services/observability_server.py` - Added new API endpoints:
  - `/pipeline-run-events` - Fetch all events for a pipeline run
  - `/active-pipeline-runs` - List active pipeline runs
- `web_ui/src/components/NavigationTabs.jsx` - Removed old "Pipeline View" tab, kept only "Pipeline Runs"

### Deprecated Endpoints
- `/current-pipeline` - Still exists for backward compatibility but is no longer used by the web UI

## Key Differences

### Old Pipeline View
- Showed generic pipeline stages from YAML configuration
- Limited real-time visibility
- No connection to actual execution events
- Basic ReactFlow visualization
- Manual refresh required

### New Pipeline Run View
- Shows actual pipeline runs with real issue data
- Complete event traceability via `pipeline_run_id`
- Chronological flowchart of all events (decision events, agent executions, Claude logs)
- Review cycle detection and horizontal visualization
- Candy stripe animation for active agents
- Real-time WebSocket updates
- Hover tooltips with metadata
- Issue-specific execution history

## Migration Guide

### For Users
1. Navigate to the new "Pipeline Runs" tab (replaces "Pipeline View")
2. Select an active pipeline run from the sidebar
3. View the complete chronological flowchart
4. Hover over nodes for detailed information
5. Watch real-time updates as agents execute

### For Developers
If you have any custom code referencing the old pipeline view:

**Old approach:**
```javascript
fetch('/current-pipeline')
```

**New approach:**
```javascript
// Get active pipeline runs
fetch('/active-pipeline-runs')

// Get events for a specific run
fetch(`/pipeline-run-events?pipeline_run_id=${runId}`)
```

## Benefits of New Approach

1. **Complete Traceability**: Every event is linked via `pipeline_run_id`
2. **Issue-Specific**: View the exact journey of each issue
3. **Real-time Updates**: WebSocket integration for live status
4. **Better Visualization**: Chronological flow with review cycle detection
5. **Debugging Support**: See decision reasoning and execution details
6. **Historical Analysis**: Query completed pipeline runs (future enhancement)

## Backward Compatibility

The `/current-pipeline` endpoint remains available but is deprecated. It will be removed in a future release once all consumers have migrated.

## Testing

After deploying these changes:

1. Start the orchestrator with a test issue
2. Navigate to "Pipeline Runs" tab
3. Verify the pipeline run appears in the sidebar
4. Click to view the flowchart
5. Verify nodes show in chronological order
6. Verify active agents show candy stripe animation
7. Test hover tooltips display metadata

## Rollback

If you need to rollback:

1. Restore `web_ui/src/routes/pipeline.jsx` from git history
2. Update `NavigationTabs.jsx` to include both tabs
3. The old `/current-pipeline` endpoint is still available

## Future Considerations

The new pipeline run approach enables:
- Historical pipeline run analysis
- Performance metrics per stage
- Comparison of multiple runs
- Export capabilities
- Advanced filtering and search

See `PIPELINE_RUN_VISUALIZATION.md` for complete feature documentation.

## Questions?

For questions or issues, refer to:
- `PIPELINE_RUN_IMPLEMENTATION.md` - Backend implementation
- `PIPELINE_RUN_VISUALIZATION.md` - Frontend implementation and usage
