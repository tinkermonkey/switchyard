import { Link } from '@tanstack/react-router'
import { Activity, GitBranch, Sparkles } from 'lucide-react'
import Header from './Header'
import AgentState from './AgentState'
import LiveLogs from './LiveLogs'
import EventTimeline from './EventTimeline'

export default function Dashboard() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />

      <div className="flex gap-3 my-3">
        <Link
          to="/"
          activeProps={{
            className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
          }}
          inactiveProps={{
            className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          }}
        >
          <Activity className="inline w-4 h-4 mr-2" />
          Dashboard
        </Link>
        <Link
          to="/pipeline"
          activeProps={{
            className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
          }}
          inactiveProps={{
            className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          }}
        >
          <GitBranch className="inline w-4 h-4 mr-2" />
          Pipeline View
        </Link>
        <Link
          to="/review-learning"
          activeProps={{
            className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
          }}
          inactiveProps={{
            className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
          }}
        >
          <Sparkles className="inline w-4 h-4 mr-2" />
          Review Learning
        </Link>
      </div>

      <AgentState />
      <LiveLogs />
      <EventTimeline />
    </div>
  )
}
