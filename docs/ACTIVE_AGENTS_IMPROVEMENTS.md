# Active Agents Display Improvements

## Overview
Enhanced the ActiveAgents component to display agent information more clearly and added branch context to help track which feature branch an agent is working on.

## Changes Made

### 1. Frontend Changes

#### ActiveAgents.jsx
**Removed Duplicate Agent Name**
- The agent name was being displayed twice (once in the title, once in the body)
- Kept the name in the HeaderBox title (displayed in ALL CAPS in the header)
- Removed the redundant display from the badge section

**Added Branch Name Display**
- Added purple badge showing the current branch name when available
- Styled consistently with other badges (issue number, project, native)
- Border and dark mode support for better visibility

**Fixed Runtime Display (NaN Issue)**
- Enhanced `formatDuration()` function to handle multiple timestamp formats
- Now supports: Unix timestamps (seconds), Unix timestamps (milliseconds), ISO date strings
- Added validation to check for invalid timestamps
- Added debug logging to help identify timestamp format issues
- Returns 'unknown' gracefully for invalid inputs

#### stateHelpers.js
**Enhanced formatDuration()**
```javascript
// Now handles multiple formats:
- Unix timestamp in seconds (< 10000000000)
- Unix timestamp in milliseconds (> 10000000000)
- ISO date strings
- Invalid/null values
```

**Added branch_name to Agent State**
- Updated `deriveActiveAgentsFromEvents()` to extract `branch_name` from event data
- Branch name is now available in the agent object alongside project, issue_number, etc.

### 2. Backend Changes

#### monitoring/observability.py
**Updated emit_agent_initialized()**
- Added optional `branch_name` parameter
- Includes branch_name in event data when available
- Maintains backward compatibility (branch_name is optional)

```python
def emit_agent_initialized(self, agent: str, task_id: str, project: str,
                          config: Dict[str, Any], branch_name: Optional[str] = None):
```

#### services/agent_executor.py
**Moved Agent Initialization Event**
- Moved `emit_agent_initialized()` call to after workspace preparation
- This ensures branch_name is available from the workspace context
- Extracts `branch_name` from workspace preparation result
- Passes branch_name to the event emission

**Execution Flow:**
1. Create agent instance
2. Prepare workspace (creates/checks out feature branch)
3. Extract branch_name from prep_result
4. Emit agent_initialized event WITH branch_name
5. Continue with agent execution

## Visual Changes

### Before
```
┌─────────────────────────────────────┐
│ Product Manager Agent               │ ← Title
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  │
│ ⚡ Product Manager Agent            │ ← Duplicate!
│ [project] [#123]                    │
│ Running: NaN                         │ ← Bug!
└─────────────────────────────────────┘
```

### After
```
┌─────────────────────────────────────┐
│ PRODUCT MANAGER AGENT               │ ← Title (ALL CAPS)
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  │ ← Status bar
│ [project] [feature/123] [#123]      │ ← Branch badge added!
│ Running: 3m 45s                      │ ← Fixed!
└─────────────────────────────────────┘
```

## Benefits

1. **Cleaner UI**: Removed duplicate agent name display
2. **Branch Context**: Users can now see which branch an agent is working on
3. **Robust Timestamps**: Fixed NaN display by handling multiple timestamp formats
4. **Better Debugging**: Added console logging for timestamp format issues
5. **Feature Branch Tracking**: Easy to track agents working on feature branches vs discussions

## Badge Hierarchy

The badges now display in this order:
1. **Project name** - Grey badge with border
2. **Branch name** - Purple badge (when available)
3. **Issue number** - Blue badge (when available)
4. **Native flag** - Purple badge (for non-containerized agents)

## Testing

### Frontend
- ✅ Build succeeds with no errors
- ✅ No duplicate agent names
- ✅ Branch name displays when available
- ✅ Timestamp formats handled correctly

### Backend
- Branch name is captured from workspace preparation
- Event includes branch_name in data payload
- Backward compatible (optional parameter)

## Future Enhancements

Potential improvements:
- Add branch status indicator (ahead/behind origin)
- Link branch name to GitHub branch view
- Show commit SHA or last commit message
- Differentiate between feature branches, discussion branches, and main branch work
- Add branch age/staleness indicator
