# State Management Architecture

This document describes the centralized state management architecture for the observability dashboard.

## Overview

The application uses a **layered context architecture** that separates concerns by domain:

- **Real-time data** via WebSocket (SocketContext)
- **System health** via polling (SystemStateContext)
- **Project data** via polling (ProjectStateContext)
- **Agent state** derived from events (AgentStateContext)

## Architecture Layers

```
┌─────────────────────────────────────────────────┐
│           AppStateProvider (Root)               │
│  ┌───────────────────────────────────────────┐ │
│  │      SocketContext (Real-time)            │ │
│  │  - Events, Logs, Real-time stats          │ │
│  └───────────────────────────────────────────┘ │
│  ┌───────────────────────────────────────────┐ │
│  │    SystemStateContext (Polling)           │ │
│  │  - Health checks, Circuit breakers        │ │
│  └───────────────────────────────────────────┘ │
│  ┌───────────────────────────────────────────┐ │
│  │   ProjectStateContext (Cached/Polling)    │ │
│  │  - Projects data, configurations          │ │
│  └───────────────────────────────────────────┘ │
│  ┌───────────────────────────────────────────┐ │
│  │   AgentStateContext (Derived + API)       │ │
│  │  - Active agents, agent operations        │ │
│  └───────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

## Directory Structure

```
web_ui/src/
├── contexts/              # State providers
│   ├── SocketContext.jsx          # WebSocket real-time data
│   ├── ThemeContext.jsx            # Theme state
│   ├── SystemStateContext.jsx      # Health & circuit breakers
│   ├── ProjectStateContext.jsx     # Project data
│   ├── AgentStateContext.jsx       # Agent state & operations
│   ├── AppStateProvider.jsx        # Root provider
│   └── index.js                    # Barrel exports
├── hooks/                 # Selector hooks
│   ├── useSystemHealth.js          # System health data
│   ├── useCircuitBreakers.js       # Circuit breaker data
│   ├── useProjects.js              # Project data
│   ├── useActiveAgents.js          # Active agent data
│   ├── useAgentActions.js          # Agent operations
│   └── index.js                    # Barrel exports
├── services/              # API clients
│   ├── api.js                      # Base API client
│   ├── systemApi.js                # Health & CB endpoints
│   ├── projectApi.js               # Project endpoints
│   ├── agentApi.js                 # Agent endpoints
│   ├── reviewApi.js                # Review endpoints
│   └── index.js                    # Barrel exports
└── utils/                 # Helper utilities
    ├── polling.js                  # Polling utilities
    └── stateHelpers.js             # State transformations
```

## Usage Patterns

### Basic Hook Usage

```jsx
import { useSystemHealth } from '../hooks/useSystemHealth'

function MyComponent() {
  const { 
    systemHealth, 
    isHealthy, 
    unhealthyComponents,
    loading 
  } = useSystemHealth()
  
  return (
    <div>
      {isHealthy ? 'All Systems Go!' : 'Issues Detected'}
    </div>
  )
}
```

### Multiple Hooks

```jsx
import { useSystemHealth } from '../hooks/useSystemHealth'
import { useCircuitBreakers } from '../hooks/useCircuitBreakers'
import { useActiveAgents } from '../hooks/useActiveAgents'

function Dashboard() {
  const { isHealthy } = useSystemHealth()
  const { hasOpenBreakers } = useCircuitBreakers()
  const { agents, agentCount } = useActiveAgents()
  
  return (
    <div>
      <p>System: {isHealthy ? 'Healthy' : 'Unhealthy'}</p>
      <p>Breakers: {hasOpenBreakers ? 'Some Open' : 'All Closed'}</p>
      <p>Active Agents: {agentCount}</p>
    </div>
  )
}
```

### Agent Operations

```jsx
import { useActiveAgents } from '../hooks/useActiveAgents'
import { useAgentActions } from '../hooks/useAgentActions'

function AgentList() {
  const { agents } = useActiveAgents()
  const { killAgent, isKillingAgent, error } = useAgentActions()
  
  const handleKill = async (containerName) => {
    try {
      await killAgent(containerName)
    } catch (err) {
      console.error('Failed to kill agent:', err)
    }
  }
  
  return (
    <div>
      {agents.map(agent => (
        <button 
          onClick={() => handleKill(agent.container_name)}
          disabled={isKillingAgent(agent.container_name)}
        >
          Kill {agent.agent}
        </button>
      ))}
    </div>
  )
}
```

## Hooks API Reference

### useSystemHealth()

Returns system health data and status flags.

```typescript
{
  systemHealth: Object,          // Raw health data
  checks: Object,                // Individual health checks
  loading: boolean,
  error: string | null,
  isHealthy: boolean,
  isDegraded: boolean,
  isUnhealthy: boolean,
  isStarting: boolean,
  status: string,
  unhealthyComponents: Array,
  unhealthyCount: number,
  refresh: () => Promise<void>
}
```

### useCircuitBreakers()

Returns circuit breaker state and summary.

```typescript
{
  circuitBreakers: Array,        // All breakers
  summary: Object,               // { open, half_open, healthy }
  loading: boolean,
  error: string | null,
  hasOpenBreakers: boolean,
  hasHalfOpenBreakers: boolean,
  allHealthy: boolean,
  problematicBreakers: Array,    // Non-closed breakers
  openCount: number,
  halfOpenCount: number,
  healthyCount: number,
  refresh: () => Promise<void>
}
```

### useProjects()

Returns project data and lookup helpers.

```typescript
{
  projects: Array,               // All projects
  projectsById: Object,          // Indexed by name
  loading: boolean,
  error: string | null,
  getProject: (name) => Object,
  projectCount: number,
  hasProjects: boolean,
  refresh: () => Promise<void>
}
```

### useActiveAgents()

Returns active agent data and groupings.

```typescript
{
  agents: Array,                 // All active agents
  agentCount: number,
  hasActiveAgents: boolean,
  agentsByProject: Object,       // Grouped by project
  agentStats: Object            // { containerized, native }
}
```

### useAgentActions()

Returns agent operation functions.

```typescript
{
  killAgent: (containerName) => Promise<void>,
  isKillingAgent: (containerName) => boolean,
  isKillingAny: boolean,
  error: string | null,
  clearError: () => void,
  hasError: boolean
}
```

## Polling Configuration

Polling intervals are centralized in `utils/polling.js`:

```javascript
export const POLLING_INTERVALS = {
  HEALTH_CHECK: 10000,        // 10 seconds
  CIRCUIT_BREAKERS: 5000,     // 5 seconds
  PROJECTS: 30000,            // 30 seconds
  SYSTEM_STATUS: 15000,       // 15 seconds
}
```

## Key Design Principles

1. **Single Responsibility**: Each context manages one domain
2. **Semantic Organization**: Data organized by business domain, not API endpoint
3. **Derived State**: Compute from WebSocket events where possible
4. **Smart Caching**: Reduce redundant API calls
5. **Composable Hooks**: Components consume only what they need
6. **Selective Re-renders**: Only affected components re-render

## Benefits

- ✅ **No Prop Drilling**: Access state from any component
- ✅ **Consistent Polling**: All health checks in one place
- ✅ **Performance**: Shared state = one API call serves all
- ✅ **Maintainability**: Change API endpoints in one place
- ✅ **Testing**: Mock entire domains easily
- ✅ **Type Safety**: Clear interfaces for all hooks
- ✅ **Real-time + REST**: Seamlessly blend WebSocket and HTTP

## Migration Guide

### Before (Scattered State)

```jsx
function Header() {
  const [health, setHealth] = useState(null)
  
  useEffect(() => {
    const fetchHealth = async () => {
      const res = await fetch('/health')
      const data = await res.json()
      setHealth(data)
    }
    
    fetchHealth()
    const interval = setInterval(fetchHealth, 10000)
    return () => clearInterval(interval)
  }, [])
  
  return <div>{health?.status}</div>
}
```

### After (Centralized State)

```jsx
import { useSystemHealth } from '../hooks/useSystemHealth'

function Header() {
  const { systemHealth, isHealthy } = useSystemHealth()
  
  return <div>{systemHealth?.status}</div>
}
```

## Future Enhancements

- Add request deduplication for simultaneous API calls
- Implement optimistic updates for mutations
- Add request caching with TTL
- Add state persistence to localStorage
- Add retry logic with exponential backoff
- Add connection status indicators
