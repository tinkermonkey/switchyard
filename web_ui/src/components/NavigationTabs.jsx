import { Link } from '@tanstack/react-router'
import { Activity, FolderGit2, Workflow, Bug, Stethoscope, Code2, BarChart2, Sliders } from 'lucide-react'

export default function NavigationTabs() {
  return (
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
        to="/pipeline-run-debug"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <Bug className="inline w-4 h-4 mr-2" />
        Pipeline Run Debug
      </Link>
      <Link
        to="/projects"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <FolderGit2 className="inline w-4 h-4 mr-2" />
        Projects
      </Link>

        <div> | </div>

      <Link
        to="/pipeline-run"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <Workflow className="inline w-4 h-4 mr-2" />
        Pipeline Run Graphs (beta)
      </Link>
      <Link
        to="/layout-sandbox"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <Sliders className="inline w-4 h-4 mr-2" />
        Layout Sandbox
      </Link>

        <div> | </div>

      <Link
        to="/agent-metrics"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <BarChart2 className="inline w-4 h-4 mr-2" />
        Agent Metrics
      </Link>
      <Link
        to="/cycle-metrics"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <BarChart2 className="inline w-4 h-4 mr-2" />
        Cycle Metrics
      </Link>
    </div>
  )
}

