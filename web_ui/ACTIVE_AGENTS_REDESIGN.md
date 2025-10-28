# Active Agents Redesign

## Problem Statement

The current `ActiveAgents` component and `useActiveAgents` hook have accuracy issues:

### Current Implementation Issues
1. **Event-based Detection**: Derives active agents from the last 50 WebSocket events
   - Events can be lost if buffer overflows
   - No guaranteed ordering or completeness
   - Relies on `deriveActiveAgentsFromEvents()` which may have logic bugs

2. **No Source of Truth**:
   - If `agent_initialized` event is not in the 50-event buffer, agent is invisible
   - If `agent_completed` event is missed, agent shows as "active" indefinitely
   - Race conditions between event arrival and state updates

3. **Disconnected from Pipeline Runs**:
   - Pipeline runs are the actual orchestration mechanism
   - Current implementation doesn't leverage this structured data

## Proposed Solution

### Use Pipeline Runs as Source of Truth

**Architecture:**
```
Active Pipeline Runs (Elasticsearch)
  ↓
Pipeline Run Events (agent_lifecycle events)
  ↓
Active Agent Detection (last agent_initialized without agent_completed)
  ↓
UI Display (ActiveAgents component)
```

### Data Flow

1. **Backend API** (already exists):
   - `/active-pipeline-runs` - Returns all active pipeline runs
   - `/pipeline-run-events?pipeline_run_id=X` - Returns all events for a run

2. **New Frontend Hook** (`useActivePipelineAgents`):
   ```javascript
   // Poll /active-pipeline-runs every 5 seconds
   // For each pipeline run, track the latest agent execution state
   // Return active agents with complete context
   ```

3. **Agent Lifecycle Events** (from Elasticsearch):
   ```json
   {
     "event_type": "agent_initialized",
     "agent": "senior_software_engineer",
     "container_name": "claude-agent-...",
     "branch_name": "feature/issue-1-core-implementation",
     "project": "codetoreum",
     "issue_number": 20,
     "pipeline_run_id": "64d89355-c3e9-4f59-93c8-4a4d14a731ee",
     "timestamp": "2025-10-28T09:28:08.739043Z"
   }
   ```

### Implementation Plan

#### 1. Create New Hook: `useActivePipelineAgents`

**File:** `web_ui/src/hooks/useActivePipelineAgents.js`

```javascript
/**
 * Hook to fetch active agents from pipeline runs (source of truth)
 * Polls /active-pipeline-runs and derives agent status from events
 */
export function useActivePipelineAgents() {
  const [loading, setLoading] = useState(true)
  const [agents, setAgents] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    // Poll every 5 seconds
    const fetchActiveAgents = async () => {
      try {
        // 1. Get all active pipeline runs
        const pipelinesRes = await fetch('/active-pipeline-runs')
        const { runs } = await pipelinesRes.json()

        // 2. For each pipeline, get the latest agent state
        const agentPromises = runs.map(async (run) => {
          const eventsRes = await fetch(`/pipeline-run-events?pipeline_run_id=${run.id}`)
          const { events } = await eventsRes.json()

          // Find the most recent agent_initialized without a corresponding completed/failed
          const agentLifecycleEvents = events.filter(e =>
            e.event_category === 'agent_lifecycle'
          )

          // Group by agent to find active ones
          const agentMap = {}
          for (const event of agentLifecycleEvents) {
            const key = event.agent
            if (!agentMap[key]) agentMap[key] = []
            agentMap[key].push(event)
          }

          // Find agents that are initialized but not completed/failed
          const activeAgents = []
          for (const [agentName, agentEvents] of Object.entries(agentMap)) {
            const sorted = agentEvents.sort((a, b) =>
              new Date(b.timestamp) - new Date(a.timestamp)
            )
            const latest = sorted[0]

            if (latest.event_type === 'agent_initialized') {
              // Check if there's a completion event after this
              const hasCompletion = sorted.some(e =>
                e.event_type === 'agent_completed' || e.event_type === 'agent_failed'
              )

              if (!hasCompletion) {
                activeAgents.push({
                  agent: agentName,
                  project: run.project,
                  issue_number: run.issue_number,
                  branch_name: latest.branch_name,
                  container_name: latest.container_name,
                  is_containerized: !!latest.container_name,
                  started_at: latest.timestamp,
                  pipeline_run_id: run.id,
                  board: run.board,
                  issue_title: run.issue_title,
                })
              }
            }
          }

          return activeAgents
        })

        const allAgents = (await Promise.all(agentPromises)).flat()
        setAgents(allAgents)
        setLoading(false)
      } catch (err) {
        console.error('Error fetching active agents:', err)
        setError(err.message)
        setLoading(false)
      }
    }

    fetchActiveAgents()
    const interval = setInterval(fetchActiveAgents, 5000)
    return () => clearInterval(interval)
  }, [])

  return {
    agents,
    agentCount: agents.length,
    hasActiveAgents: agents.length > 0,
    loading,
    error,
  }
}
```

#### 2. Backend Enhancement (Optional)

Add a dedicated endpoint to simplify the frontend:

**Endpoint:** `GET /api/active-agents`

```python
@app.route('/api/active-agents')
def get_active_agents():
    """
    Get all currently active agents across all pipeline runs
    Returns agents that have been initialized but not completed/failed
    """
    try:
        # Get all active pipeline runs
        pipeline_runs_query = {
            "query": {"term": {"status": "active"}},
            "size": 100
        }
        pipeline_runs = es_client.search(
            index="pipeline-runs",
            body=pipeline_runs_query
        )

        active_agents = []

        for pipeline_hit in pipeline_runs['hits']['hits']:
            pipeline_run = pipeline_hit['_source']
            pipeline_run_id = pipeline_run['id']

            # Get agent lifecycle events for this pipeline
            agent_events_query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"pipeline_run_id.keyword": pipeline_run_id}},
                            {"terms": {"event_type": ["agent_initialized", "agent_completed", "agent_failed"]}}
                        ]
                    }
                },
                "sort": [{"timestamp": "asc"}],
                "size": 1000
            }

            events_result = es_client.search(
                index="agent-events-*",
                body=agent_events_query
            )

            # Track agent states
            agent_states = {}
            for hit in events_result['hits']['hits']:
                event = hit['_source']
                agent_key = f"{event['agent']}_{event.get('task_id', '')}"

                if event['event_type'] == 'agent_initialized':
                    agent_states[agent_key] = {
                        'agent': event['agent'],
                        'status': 'running',
                        'container_name': event.get('container_name'),
                        'branch_name': event.get('branch_name'),
                        'started_at': event['timestamp'],
                        'project': pipeline_run['project'],
                        'issue_number': pipeline_run['issue_number'],
                        'issue_title': pipeline_run['issue_title'],
                        'board': pipeline_run['board'],
                        'pipeline_run_id': pipeline_run_id,
                        'is_containerized': bool(event.get('container_name')),
                    }
                elif event['event_type'] in ['agent_completed', 'agent_failed']:
                    if agent_key in agent_states:
                        agent_states[agent_key]['status'] = 'completed' if event['event_type'] == 'agent_completed' else 'failed'

            # Add running agents to result
            for agent_data in agent_states.values():
                if agent_data['status'] == 'running':
                    active_agents.append(agent_data)

        return jsonify({
            'success': True,
            'agents': active_agents,
            'count': len(active_agents)
        })

    except Exception as e:
        logger.error(f"Error fetching active agents: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'agents': []
        }), 500
```

#### 3. Update ActiveAgents Component

Replace `useActiveAgents()` with `useActivePipelineAgents()`:

```javascript
// In ActiveAgents.jsx
import { useActivePipelineAgents } from '../hooks/useActivePipelineAgents'

const ActiveAgents = ({ ContainerComponent = 'div', containerClassName }) => {
  const { agents, loading, error } = useActivePipelineAgents()
  // ... rest of component unchanged
}
```

#### 4. Deprecate Old Approach

- Keep `deriveActiveAgentsFromEvents()` for backward compatibility
- Add deprecation warning
- Remove `AgentStateContext` dependency from ActiveAgents
- Eventually remove event-based derivation entirely

### Benefits

1. **Accurate**: Pipeline runs are the source of truth
2. **Complete**: All active agents are included, not just last 50 events
3. **Reliable**: Elasticsearch queries are idempotent and consistent
4. **Debuggable**: Clear data lineage from pipeline → events → agents
5. **Performant**: Polling every 5s is acceptable, can add WebSocket updates later

### Migration Strategy

1. **Phase 1**: Add new backend endpoint `/api/active-agents`
2. **Phase 2**: Create `useActivePipelineAgents` hook (parallel to old hook)
3. **Phase 3**: Update ActiveAgents component to use new hook
4. **Phase 4**: Test thoroughly and compare with old implementation
5. **Phase 5**: Remove old event-based derivation code

### Testing

- Verify all active agents appear correctly
- Test with multiple concurrent pipelines
- Test agent transitions (start → running → complete)
- Test containerized vs native agents
- Test error handling when pipeline events are missing

## Conclusion

This redesign shifts from **event-stream inference** to **pipeline-centric querying**, providing a more reliable and maintainable solution for tracking active agents.
