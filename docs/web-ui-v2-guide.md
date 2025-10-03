# Web UI v2 - Quick Start Guide

## Overview

The new React-based observability UI provides real-time monitoring of the orchestrator with a modern, responsive interface that matches the GitHub dark theme color palette.

## Running the UI

### Development Mode

```bash
cd web_ui_v2
npm install
npm run dev
```

The UI will be available at `http://localhost:3000`

### Production Mode with Docker

```bash
# Build and run all services
docker-compose up web-ui

# Or build individually
docker-compose build web-ui
docker-compose up -d web-ui
```

The UI will be served at `http://localhost:3000`

## Features

### Dashboard View (`/`)

**Header Section:**
- Connection status indicator (green = connected, red = disconnected)
- Dark/Light mode toggle

**Statistics Cards:**
- Total Events - Count of all events received
- Active Tasks - Number of tasks currently running
- Total Tokens - Cumulative token usage across all Claude API calls
- Avg API Latency - Average response time for Claude API calls

**Live Claude Logs:**
- Real-time stream of Claude API interactions
- Markdown rendering for text responses
- Tool use visualization (Bash, Read, Grep, Edit, Write, etc.)
- TodoWrite rendering with checkbox states
- Token usage metrics
- Expandable details modal for full event data
- Auto-scroll toggle
- Clear logs button

**Event Timeline:**
- Specialized event cards for different event types:
  - Task Received - Shows project, board, issue details
  - Agent Initialized - Shows model, timeout, MCP servers
  - Prompt Constructed - Shows prompt length, estimated tokens, expandable full prompt
  - Claude API Call Started - Shows loading progress
  - Claude API Call Completed - Shows input/output tokens, cost estimate
  - Agent Completed/Failed - Shows duration, success status
- Expandable details for all events
- Auto-scroll capability
- Clear events button

### Pipeline View (`/pipeline`)

**Interactive Flow Diagram:**
- Visual representation of agent pipeline
- Nodes colored by stage:
  - Blue - Input (Idea Researcher)
  - Green - Requirements/PM stages
  - Purple - Architecture
  - Orange - Development
  - Yellow - QA/Testing
  - Green border - Complete
- Animated edges showing data flow
- Interactive controls:
  - Zoom in/out
  - Pan around canvas
  - Reset view
- Mini-map for navigation
- Background grid

## Architecture

### Tech Stack
- React 19
- Vite for build tooling
- TanStack Router for routing
- Tailwind CSS + Flowbite for styling
- Lucide React for icons
- ReactFlow for pipeline visualization
- Socket.IO for WebSocket communication
- React Markdown for rendering Claude responses

### Context Providers
- **ThemeContext** - Dark/light mode with localStorage persistence
- **SocketContext** - WebSocket connection, event/log management, stats tracking

### File Structure
```
web_ui_v2/
├── src/
│   ├── routes/
│   │   ├── __root.jsx        # Root layout with providers
│   │   ├── index.jsx          # Dashboard route
│   │   └── pipeline.jsx       # Pipeline view route
│   ├── contexts/
│   │   ├── ThemeContext.jsx   # Theme management
│   │   └── SocketContext.jsx  # WebSocket & state
│   ├── components/
│   │   ├── Dashboard.jsx      # Main dashboard
│   │   ├── Header.jsx         # Header with status
│   │   ├── StatsCards.jsx     # Metrics cards
│   │   ├── LiveLogs.jsx       # Claude logs stream
│   │   ├── EventTimeline.jsx  # Event cards
│   │   ├── PipelineView.jsx   # ReactFlow diagram
│   │   └── Modal.jsx          # Reusable modal
│   ├── index.css              # Tailwind + custom styles
│   └── main.jsx               # App entry point
├── tailwind.config.js         # Tailwind with GitHub colors
├── vite.config.js             # Vite with proxy config
├── Dockerfile                 # Production build
└── nginx.conf                 # Nginx proxy config
```

## Color Palette

Matching the original GitHub dark theme:

| Color | Hex | Usage |
|-------|-----|-------|
| Canvas | #0d1117 | Background |
| Canvas Subtle | #161b22 | Cards, panels |
| Border | #30363d | Borders |
| Border Muted | #21262d | Subtle borders |
| Foreground | #c9d1d9 | Primary text |
| Foreground Muted | #8b949e | Secondary text |
| Foreground Subtle | #6e7681 | Tertiary text |
| Accent Primary | #58a6ff | Links, highlights |
| Accent Emphasis | #1f6feb | Stronger accents |
| Success | #238636 | Success states |
| Danger | #da3633 | Errors |
| Warning | #9e6a03 | Warnings |
| Severe | #da7633 | Severe warnings |
| Done | #8957e5 | Completion |

## WebSocket Events

The UI subscribes to:
- `agent_event` - Agent lifecycle events (task received, initialized, completed, failed)
- `claude_stream_event` - Real-time Claude API events (assistant, user, tool use)

## API Endpoints

- `GET /history?count=N` - Fetch recent agent events (max 500)
- `GET /claude-logs-history?count=N` - Fetch recent Claude logs (max 500)
- `WS /socket.io` - WebSocket connection to observability server

## Development Tips

### Adding New Event Types

1. Add event type to `EventTimeline.jsx` switch statement
2. Create specialized render function (e.g., `TaskReceivedEvent`)
3. Use `EventCard` wrapper component for consistent styling

### Customizing Theme

1. Update `tailwind.config.js` colors
2. Modify theme in `ThemeContext.jsx`
3. Adjust CSS in `index.css` if needed

### Adding New Routes

1. Create file in `src/routes/` (e.g., `agents.jsx`)
2. Export route using `createFileRoute`
3. Router will auto-detect and add route

## Troubleshooting

### WebSocket not connecting
- Ensure observability server is running on port 5001
- Check browser console for connection errors
- Verify proxy configuration in `vite.config.js` (dev) or `nginx.conf` (prod)

### Events not appearing
- Check Redis is running and accessible
- Verify orchestrator is emitting events
- Check observability server logs

### Build errors
- Run `npm install` to ensure all dependencies are installed
- Clear `node_modules` and reinstall if needed
- Check for TypeScript/ESLint errors

### Docker issues
- Rebuild image: `docker-compose build web-ui`
- Check logs: `docker-compose logs web-ui`
- Verify nginx config syntax
