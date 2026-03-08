import { Link } from '@tanstack/react-router'
import { Activity, FolderGit2, Workflow, BarChart2, TrendingUp, FileBarChart2, ClipboardList } from 'lucide-react'

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
        Pipeline Runs
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
      <Link
        to="/project-metrics"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <TrendingUp className="inline w-4 h-4 mr-2" />
        Project Metrics
      </Link>

        <div> | </div>

      <Link
        to="/pipeline-reports"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <FileBarChart2 className="inline w-4 h-4 mr-2" />
        Pipeline Reports
      </Link>
      <Link
        to="/recommendation-reports"
        activeProps={{
          className: "px-4 py-2 bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-sm text-white"
        }}
        inactiveProps={{
          className: "px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        }}
      >
        <ClipboardList className="inline w-4 h-4 mr-2" />
        Recommendation Reports
      </Link>
    </div>
  )
}
