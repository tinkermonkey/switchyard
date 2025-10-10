# Pipeline Run Visualization Implementation

## Overview

This document describes the new Pipeline Run visualization feature that provides a comprehensive flowchart view of issue journeys through the orchestrator workflow pipeline.

## Features Implemented

### 1. New API Endpoints

#### `/pipeline-run-events`
Fetches all events for a specific pipeline run in chronological order.

**Query Parameters:**
- `pipeline_run_id` (required): The UUID of the pipeline run

**Response:**
```json
{
  "success": true,
  "pipeline_run": {
    "id": "uuid",
    "issue_number": 123,
    "issue_title": "Feature request",
    "issue_url": "https://github.com/...",
    "project": "project-name",
    "board": "board-name",
    "started_at": "2025-10-09T10:00:00Z",
    "ended_at": "2025-10-09T11:30:00Z",
    "status": "completed"
  },
  "events": [
    {
      "event_category": "decision",
      "decision_type": "agent_routing_decision",
      "timestamp": "2025-10-09T10:00:01Z",
      "reason": "Status maps to agent",
      ...
    },
    {
      "event_category": "agent_lifecycle",
      "event_type": "agent_initialized",
      "agent": "product_manager_agent",
      "task_id": "task-123",
      "timestamp": "2025-10-09T10:00:05Z",
      ...
    },
    {
      "event_category": "claude_log",
      "timestamp": "2025-10-09T10:00:10Z",
      ...
    }
  ],
  "event_count": 150
}
```

**Event Categories:**
- `decision`: Decision events from decision_events.py
- `agent_lifecycle`: Agent initialization, completion, failure events
- `claude_log`: Detailed execution logs from Claude streams

#### `/active-pipeline-runs`
Fetches all currently active pipeline runs.

**Response:**
```json
{
  "success": true,
  "runs": [
    {
      "id": "uuid",
      "issue_number": 123,
      "issue_title": "Feature request",
      "project": "project-name",
      "board": "board-name",
      "started_at": "2025-10-09T10:00:00Z",
      "status": "active"
    }
  ],
  "count": 1
}
```

### 2. New React Component: Pipeline Run View

#### Location
`web_ui/src/routes/pipeline-run.jsx`

#### Features

**Pipeline Run Selector**
- Displays all active pipeline runs in a sidebar
- Shows issue title, project, issue number, and start time
- Click to select and view the flowchart for a specific run

**Chronological Flowchart**
- Top-to-bottom visualization showing the complete journey
- Starts with a "Pipeline Started" node
- Shows all decision events and agent executions in order
- Ends with a "Pipeline Completed" node (if completed)

**Node Types**

1. **Pipeline Created** (Green)
   - Marks the beginning of the pipeline run
   - Shows start timestamp

2. **Decision Events** (Orange)
   - Shows routing decisions, task queuing, etc.
   - Displays decision type and reason

3. **Agent Execution** (Blue/Green/Red)
   - Blue with candy stripes: Currently running
   - Green: Completed successfully
   - Red: Failed
   - Gray: Pending

4. **Review Feedback** (Purple)
   - Shows review feedback loops
   - Rendered horizontally when multiple iterations exist

5. **Human Feedback** (Pink)
   - Human intervention events

6. **Pipeline Completed** (Indigo)
   - Marks successful completion
   - Shows end timestamp

**Review Cycle Visualization**

When an agent has multiple executions (review cycles):
- First execution: Normal vertical placement
- Subsequent executions: Positioned horizontally to the right
- Feedback arrows: Dashed orange lines connecting iterations
- Visual indication: "Iteration 1", "Iteration 2", etc.

Example layout for a 2-iteration review cycle:
```
    [Agent Execution 1] ----feedback----> [Agent Execution 2]
            |                                      |
            v                                      v
         [Next Stage]                      [Next Stage]
```

**Candy Stripe Animation**

Active agents display a pulsing candy stripe animation at the top of their node:
- Diagonal stripes with transparency
- Animated movement from left to right
- Combined with pulse animation for breathing effect
- Matches the style from ActiveAgents.jsx

**Hover Tooltips**

Hovering over any node displays a tooltip with:
- Node label (title)
- Full metadata/description
- Positioned in top-right corner

**Interactive Features**

- Zoom and pan controls (ReactFlow built-in)
- MiniMap for navigation
- Auto-fit view on load
- Real-time updates via WebSocket

### 3. Navigation Integration

**Updated NavigationTabs.jsx**
- Added new "Pipeline Runs" tab with Workflow icon
- Positioned between "Pipeline View" and "Review Learning"
- Uses `/pipeline-run` route

### 4. Visual Design

**Color Scheme**
- Pipeline Started: Green (#10b981)
- Decision Events: Orange (#f59e0b)
- Agent Running: Blue (#1f6feb) with candy stripes
- Agent Completed: Green (#238636)
- Agent Failed: Red (#da3633)
- Review Feedback: Purple (#8b5cf6)
- Human Feedback: Pink (#ec4899)
- Pipeline Completed: Indigo (#6366f1)

**Legend**
- Displayed at bottom of page
- Shows all node types with color indicators
- Includes candy stripe animation example

### 5. Real-time Updates

**WebSocket Integration**
- Listens for agent lifecycle events
- Auto-refreshes pipeline run events when changes detected
- Updates active agent status in real-time
- Candy stripe animation reflects live execution status

## Technical Implementation

### Frontend Architecture

**State Management**
```javascript
const [activePipelineRuns, setActivePipelineRuns] = useState([])
const [selectedPipelineRun, setSelectedPipelineRun] = useState(null)
const [pipelineRunEvents, setPipelineRunEvents] = useState([])
const [nodes, setNodes, onNodesChange] = useNodesState([])
const [edges, setEdges, onEdgesChange] = useEdgesState([])
```

**Flow Construction Algorithm**

1. Parse all events chronologically
2. Group agent executions by agent name
3. Identify review cycles (agents with multiple executions)
4. Build nodes:
   - Start with "Pipeline Created"
   - Add decision events in order
   - Add agent executions:
     - Single execution: Vertical placement
     - Multiple executions: Horizontal layout with feedback loops
   - End with "Pipeline Completed" if status is completed
5. Build edges:
   - Sequential flow: Vertical connections
   - Review feedback: Horizontal dashed arrows
6. Apply ReactFlow layout

**Custom Node Component**
```javascript
const PipelineEventNode = ({ data }) => {
  // Renders custom styled nodes
  // Includes candy stripe animation for active agents
  // Shows icon, label, and metadata
}
```

### Backend Architecture

**Elasticsearch Queries**

The `/pipeline-run-events` endpoint queries three indices:
1. `decision-events-*`: Decision events
2. `agent-events-*`: Agent lifecycle events
3. `claude-streams-*`: Detailed execution logs

All events are filtered by `pipeline_run_id` and sorted by `timestamp`.

**Data Aggregation**

Events from multiple sources are combined and sorted chronologically to provide a unified timeline.

## Usage

### Viewing Active Pipeline Runs

1. Navigate to "Pipeline Runs" tab
2. Active runs appear in the left sidebar
3. Click on a run to view its flowchart

### Understanding the Flowchart

**Vertical Flow**: Normal sequential execution
- Each node represents a stage or decision
- Arrows show progression through pipeline

**Horizontal Flow**: Review cycles
- Multiple instances of same agent
- Dashed feedback arrows show iteration
- First iteration on left, subsequent iterations to the right

**Active Indicators**
- Candy stripe animation: Agent currently executing
- Blue highlighting: Running status
- Real-time updates via WebSocket

### Interpreting Node Colors

- **Green**: Success/Start
- **Blue with stripes**: Currently running
- **Orange**: Decision point
- **Purple**: Review feedback
- **Red**: Failure
- **Indigo**: Completion

## Benefits

### Complete Visibility
- See entire issue journey from start to finish
- Understand decision flow and agent handoffs
- Identify bottlenecks and long-running stages

### Debug Support
- Trace failures to specific agents
- See decision reasons leading to routing
- Understand review cycle iterations

### Performance Analysis
- Visual representation of execution time
- Identify agents that take longest
- See how review cycles impact duration

### Real-time Monitoring
- Watch active pipeline runs in progress
- See which agents are currently executing
- Get live updates via WebSocket

## Future Enhancements

### 1. Historical Pipeline Runs
- Add date range selector
- Show completed pipeline runs
- Search by issue number or project

### 2. Detailed Event Inspection
- Click node to show detailed event data
- Display full Claude logs in side panel
- Show tool executions and results

### 3. Metrics Integration
- Show duration for each stage
- Display success/failure rates
- Highlight performance anomalies

### 4. Comparison View
- Compare multiple pipeline runs side-by-side
- Show differences in execution paths
- Identify patterns in successful vs failed runs

### 5. Export Capabilities
- Export flowchart as image
- Download event timeline as JSON
- Generate PDF report

## Dependencies

**Frontend**
- React
- @xyflow/react (ReactFlow)
- @tanstack/react-router
- lucide-react (icons)

**Backend**
- Flask
- Elasticsearch
- Redis
- services/pipeline_run.py

## Testing

### Manual Testing

1. **Create a test issue** in a monitored project
2. **Move issue** to first agent column
3. **Navigate** to Pipeline Runs tab
4. **Verify** pipeline run appears in sidebar
5. **Click** pipeline run to view flowchart
6. **Verify** nodes show chronological progression
7. **Hover** over nodes to see tooltips
8. **Monitor** real-time updates as agents execute

### Review Cycle Testing

1. **Configure agent** with reviewer_agent
2. **Create test issue** with intentional review failure
3. **Verify** horizontal layout for review iterations
4. **Verify** feedback arrows appear
5. **Check** iteration labels (Iteration 1, 2, etc.)

### Candy Stripe Testing

1. **Start agent execution**
2. **Navigate** to Pipeline Runs
3. **Verify** active agent shows candy stripe animation
4. **Verify** animation matches ActiveAgents.jsx style
5. **Wait for completion**
6. **Verify** stripe disappears when agent completes

## Troubleshooting

### Pipeline run not appearing
- Check Redis connection
- Verify pipeline_run_id is being set in events
- Check Elasticsearch indices exist
- Verify issue has active pipeline run

### Events not showing
- Check pipeline_run_id in event data
- Verify Elasticsearch queries are working
- Check event timestamps are valid
- Verify event indices are being created

### Flowchart not rendering
- Check browser console for React errors
- Verify ReactFlow is properly imported
- Check node positions are valid
- Verify edges reference existing node IDs

### Candy stripes not animating
- Check CSS animation is defined
- Verify isActive flag is set correctly
- Check socket events are being received
- Verify agent is actually running

## Summary

The Pipeline Run visualization provides comprehensive, real-time visibility into issue workflows through the orchestrator. With chronological flowcharts, review cycle detection, and active agent indicators, operators can monitor, debug, and analyze pipeline executions with unprecedented clarity.
