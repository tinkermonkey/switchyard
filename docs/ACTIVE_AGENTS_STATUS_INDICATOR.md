# Active Agents Status Indicator Updates

## Overview
Updated the `ActiveAgents` component to display human-readable agent names and add a visual status indicator with color coding and animation.

## Changes Made

### 1. Human-Readable Agent Names
Added `formatAgentName()` utility function that converts snake_case agent names to Title Case:
```javascript
"product_manager_agent" → "Product Manager Agent"
"senior_software_engineer_agent" → "Senior Software Engineer Agent"
```

This function is used in:
- Agent card display names
- HeaderBox titles when rendered in the header
- Modal confirmation dialog

### 2. Status Indicator with Color Coding
Added visual status indicator at the top of each agent card with color-coded runtime thresholds:

**Color Coding:**
- 🟢 **Green**: < 5 minutes (normal operation)
- 🟡 **Yellow**: 5-15 minutes (warning - agent taking longer than expected)
- 🔴 **Red**: > 15 minutes (alert - agent may be stuck)

### 3. Animated Candy Stripe Effect
The status indicator includes:
- **Candy stripe pattern**: Diagonal white stripes at 45-degree angle
- **Pulsing animation**: 2-second pulse cycle for subtle breathing effect
- **Scrolling stripes**: 1-second horizontal animation to convey activity
- Combined effect creates a dynamic, living indicator

### 4. CSS Animations
Added to `index.css`:
```css
@keyframes stripes {
  0% { background-position: 0 0; }
  100% { background-position: 1rem 0; }
}
```

The stripe animation is applied inline with:
```javascript
animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite, stripes 1s linear infinite'
```

## Visual Design

### Agent Card Structure
```
┌─────────────────────────────────────┐
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  │ ← Animated status bar
│                                      │
│ ⚡ Product Manager Agent             │ ← Human-readable name
│ [project-name] [#123] [native]      │ ← Badges
│                                      │
│ Running: 3m 45s                      │ ← Runtime info
│ Container: abc123                    │
│                                      │
│ [Kill] Button                        │
└─────────────────────────────────────┘
```

## Benefits

1. **Improved Readability**: Agent names are immediately understandable
2. **Quick Status Assessment**: Color coding allows instant recognition of potential issues
3. **Activity Indication**: Animation clearly shows agents are running
4. **Progressive Warnings**: Color transitions provide early warning before agents time out
5. **Consistent UX**: Uses same formatAgentName() as AgentState component

## Files Modified

- `web_ui/src/components/ActiveAgents.jsx`
  - Updated to display status indicators for currently running agents
  - Added color-coded status badges (running, idle)
  - Integrated with real-time data from observability server

- `web_ui/src/index.css`
  - Added `@keyframes stripes` animation

## Testing

Build succeeded with no errors. The status indicator should now be visible on all active agent cards, with colors automatically adjusting based on runtime duration.

## Future Enhancements

Potential improvements:
- Make runtime thresholds configurable per agent type
- Add tooltip showing exact runtime when hovering over status bar
- Different animation patterns for different agent states (idle, working, waiting)
- Integration with agent health metrics for more accurate status
