# Migration Checklist for Other Components

Use this checklist when migrating other components to the new state management architecture.

## Components Already Migrated ✅

- [x] Header.jsx
- [x] Projects.jsx
- [x] ActiveAgents.jsx

## Components to Consider Migrating

### Components Using Health/System Data

- [ ] **Dashboard.jsx** - Already using SocketContext, minimal changes needed
- [ ] **AgentState.jsx** - Uses events from SocketContext
- [ ] **EventTimeline.jsx** - Uses events from SocketContext
- [ ] **LiveLogs.jsx** - Uses logs from SocketContext

### Components Using Project Data

- [ ] **ProjectCard.jsx** - Receives props, no direct API calls
- [ ] **WorkspaceStatus.jsx** - May have local state
- [ ] **DevContainerStatus.jsx** - May have local state

### Components Using Review Data

- [ ] **ReviewLearning.jsx** - Uses direct fetch calls to review API
  - Replace with `reviewApi` service layer
  - Consider creating `useReviewFilters()` hook

## Migration Steps

### 1. Identify Data Sources

**Before migrating a component:**

- [ ] List all `useState` declarations
- [ ] List all `useEffect` with API calls
- [ ] List all `fetch()` calls
- [ ] Note polling intervals

### 2. Choose the Right Hook

| Data Type | Hook to Use |
|-----------|-------------|
| System health | `useSystemHealth()` |
| Circuit breakers | `useCircuitBreakers()` |
| Projects | `useProjects()` |
| Active agents | `useActiveAgents()` |
| Agent operations | `useAgentActions()` |
| WebSocket events | `useSocket()` (existing) |
| Theme | `useTheme()` (existing) |

### 3. Refactor the Component

**Example transformation:**

```jsx
// BEFORE
import { useState, useEffect } from 'react'

function MyComponent() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  
  useEffect(() => {
    const fetchData = async () => {
      const res = await fetch('/api/endpoint')
      const data = await res.json()
      setData(data)
      setLoading(false)
    }
    fetchData()
    const interval = setInterval(fetchData, 30000)
    return () => clearInterval(interval)
  }, [])
  
  return <div>{loading ? 'Loading...' : data.value}</div>
}
```

```jsx
// AFTER
import { useProjects } from '../hooks/useProjects'

function MyComponent() {
  const { projects, loading } = useProjects()
  
  return <div>{loading ? 'Loading...' : projects.length}</div>
}
```

### 4. Remove Redundant Code

- [ ] Remove `useState` for API data
- [ ] Remove `useEffect` for polling
- [ ] Remove direct `fetch()` calls
- [ ] Remove manual error handling (if using context error states)

### 5. Test the Component

- [ ] Verify data loads correctly
- [ ] Check that polling still works
- [ ] Test error states
- [ ] Verify no console errors
- [ ] Check re-render behavior

## Special Cases

### ReviewLearning Component

This component needs special attention as it has its own API calls:

```jsx
// Current pattern in ReviewLearning.jsx
fetch(`${API_BASE}/api/review-filters/agents`)
fetch(`${API_BASE}/api/review-filters`)
```

**Migration approach:**

1. Already have `reviewApi` service created
2. Create optional hook: `useReviewFilters(agentFilter)`
3. Or use `reviewApi` directly in component (service layer is enough)

### Components with WebSocket Dependencies

Components using `useSocket()` don't need migration - they're already using the context pattern correctly:

```jsx
import { useSocket } from '../contexts/SocketContext'

function MyComponent() {
  const { events, logs, connected } = useSocket()
  // This is already the right pattern
}
```

## Creating New Hooks

If you find a component that needs data not covered by existing hooks:

1. **Create the API service** (if needed)
   ```javascript
   // services/myApi.js
   export const myApi = {
     async getData() {
       return apiClient.get('/api/my-endpoint')
     }
   }
   ```

2. **Create a context provider** (if state is shared)
   ```jsx
   // contexts/MyStateContext.jsx
   export function MyStateProvider({ children }) {
     // State management logic
   }
   ```

3. **Create a selector hook**
   ```javascript
   // hooks/useMyData.js
   export function useMyData() {
     const context = useContext(MyStateContext)
     // Return selected data
   }
   ```

4. **Add to AppStateProvider**
   ```jsx
   <MyStateProvider>
     {children}
   </MyStateProvider>
   ```

## Benefits Checklist

After migrating a component, verify these benefits:

- [ ] Reduced lines of code
- [ ] No more useEffect for API polling
- [ ] Shared state (multiple components use same data)
- [ ] Consistent error handling
- [ ] Single source of truth
- [ ] Easier to test
- [ ] Better separation of concerns

## Notes

- **Don't over-abstract**: Not every component needs a hook. Simple components can use service layer directly
- **Keep WebSocket separate**: Real-time data stays in SocketContext
- **Derived state**: Prefer computing from existing state over new API calls
- **Composition**: Combine multiple hooks in a single component when needed
