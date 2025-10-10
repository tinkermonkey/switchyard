# Orchestrator Web UI v2

Modern React-based observability dashboard for the Claude Code Agent Orchestrator.

## Tech Stack

- **React 19** - UI framework
- **Vite** - Build tool and dev server
- **TanStack Router** - File-based routing
- **Tailwind CSS** - Styling
- **Flowbite React** - Component library
- **Lucide React** - Icon library
- **ReactFlow** - Pipeline visualization
- **Socket.IO Client** - Real-time WebSocket communication
- **React Markdown** - Markdown rendering with GFM support

## Features

### Dashboard View
- **Real-time Event Stream** - Live updates via WebSocket
- **Agent Activity Monitoring** - Track all agent events
- **Statistics Cards** - Total events, active tasks, token usage, API latency
- **Live Claude Logs** - Stream Claude API interactions with markdown rendering
- **Event Timeline** - Specialized event cards for different event types
- **Dark/Light Mode** - Theme toggle with localStorage persistence

### Pipeline View
- **Visual Pipeline Flow** - Interactive ReactFlow diagram
- **Agent Relationships** - See how agents connect in workflows
- **Interactive Graph** - Zoom, pan, and explore agent pipelines

## Development

```bash
# Install dependencies
npm install

# Start dev server
npm run dev

# Build for production
npm run build
```

## Docker Deployment

```bash
# Build and run with docker-compose
docker-compose up web-ui

# Access at http://localhost:3000
```
