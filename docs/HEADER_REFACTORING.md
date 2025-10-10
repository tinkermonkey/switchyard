# Header Component Refactoring

## Summary

Refactored the Header component to use modular, reusable sub-components for better maintainability and cleaner code structure.

## Changes Made

### New Components Created

#### 1. **HeaderBox.jsx** - Base Component
- Provides consistent styling for all header boxes
- Props: `title`, `children`, `className`, `minWidth`
- Used by all other header components

#### 2. **HeaderStatsCard.jsx** - Stats Display
- Standard stats card (Total Events, Tokens, Latency)
- Props: `title`, `value`
- Replaces inline stat card rendering

#### 3. **HeaderSystemHealth.jsx** - System Health Display
- Shows individual health checks (GitHub, Claude, Disk, Memory)
- Displays icon + status for each check
- Prioritizes most important health indicators
- Uses `useSystemHealth()` hook

#### 4. **HeaderCircuitBreakers.jsx** - Circuit Breaker Display
- Shows individual circuit breaker states
- Displays icon + state for each breaker
- Shows up to 4 breakers, indicates if more exist
- Uses `useCircuitBreakers()` hook

#### 5. **HeaderClaudeUsage.jsx** - Claude Usage Display
- Shows weekly and session quota usage
- Progress bars with color coding (green/yellow/red)
- Displays remaining minutes for session
- Uses `useSystemHealth()` hook to access usage data

#### 6. **HeaderActiveAgents.jsx** - Active Agents Wrapper
- Wraps existing ActiveAgents component
- Maintains separation of concerns

### Header.jsx Improvements

**Before:**
- 380 lines of code
- Inline stat card rendering
- Complex nested ternary for Claude usage
- Status pills for system health and circuit breakers
- All styling inline

**After:**
- ~190 lines of code (52% reduction)
- Clean component composition
- Modular, reusable components
- Status boxes replace pills for consistency
- Individual health checks visible at a glance
- Circuit breaker states visible individually

### Visual Changes

#### Status Display Evolution

**OLD: Status Pills**
```
[Connected] [System Healthy] [All Breakers Closed]
```

**NEW: Detailed Status Boxes**
```
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ System Health│ │Circuit Break.│ │ Claude Usage │
│ ✓ GitHub  OK │ │ ✓ github Cls.│ │ Week: 45/100M│
│ ✓ Claude  OK │ │ ✓ claude Cls.│ │ ████░░░ 45%  │
│ ✓ Disk    OK │ │ ✓ disk   Cls.│ │ Sess: 12/50M │
│ ✓ Memory  OK │ │ ✓ memory Cls.│ │ ███░░░░ 24%  │
└──────────────┘ └──────────────┘ └──────────────┘
```

#### Benefits

1. **More Detailed Information**: Individual health checks visible without clicking
2. **Consistent Design**: All boxes use same HeaderBox base component
3. **Better Scalability**: Easy to add new status boxes
4. **Improved UX**: Users see specific problems at a glance
5. **Maintainability**: Each component has single responsibility
6. **Reusability**: Components can be used in other views

### Component Structure

```
Header.jsx
├── Alert Banners (system issues/CB problems)
├── Main Header Box
│   ├── Title & Connection Badge
│   └── Stats Section
│       ├── HeaderActiveAgents
│       ├── HeaderSystemHealth (NEW)
│       ├── HeaderCircuitBreakers (NEW)
│       ├── HeaderClaudeUsage (extracted)
│       ├── HeaderStatsCard (Total Events)
│       ├── HeaderStatsCard (Total Tokens)
│       └── HeaderStatsCard (Avg Latency)
```

### Code Quality Improvements

- **Separation of Concerns**: Each component handles one thing
- **DRY Principle**: HeaderBox eliminates repeated styling
- **Testability**: Individual components easier to test
- **Readability**: Header.jsx is now much easier to understand
- **Type Safety**: Clear prop interfaces for each component

### Backward Compatibility

- ✅ All existing functionality preserved
- ✅ Alert banners still work
- ✅ WebSocket connection status maintained
- ✅ All hooks remain the same
- ✅ No breaking changes to parent components

## Files Modified

- `src/components/Header.jsx` - Refactored to use new components
- `src/components/HeaderBox.jsx` - NEW
- `src/components/HeaderStatsCard.jsx` - NEW
- `src/components/HeaderSystemHealth.jsx` - NEW
- `src/components/HeaderCircuitBreakers.jsx` - NEW
- `src/components/HeaderClaudeUsage.jsx` - NEW
- `src/components/HeaderActiveAgents.jsx` - NEW

## Next Steps (Optional)

Consider creating similar box components for:
- Agent task progress
- Recent errors/warnings
- API response times by endpoint
- Resource usage trends
