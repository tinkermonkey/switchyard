import { Link } from '@tanstack/react-router'
import { FolderGit2, Workflow, BarChart2, TrendingUp, FileBarChart2, ClipboardList } from 'lucide-react'

export default function NavigationTabs() {
  return (
    <div className="flex gap-2 md:gap-3 my-3 overflow-x-auto scrollbar-hide">
      <Link
        to="/pipeline-run"
        activeProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-white"
        }}
        inactiveProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        }}
      >
        <Workflow className="inline w-4 h-4 mr-2" />
        Pipeline Runs
      </Link>
      <Link
        to="/projects"
        activeProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-white"
        }}
        inactiveProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        }}
      >
        <FolderGit2 className="inline w-4 h-4 mr-2" />
        Projects
      </Link>

        <div className="flex-shrink-0"> | </div>

      <Link
        to="/agent-metrics"
        activeProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-white"
        }}
        inactiveProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        }}
      >
        <BarChart2 className="inline w-4 h-4 mr-2" />
        Agent Metrics
      </Link>
      <Link
        to="/cycle-metrics"
        activeProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-white"
        }}
        inactiveProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        }}
      >
        <BarChart2 className="inline w-4 h-4 mr-2" />
        Cycle Metrics
      </Link>
      <Link
        to="/project-metrics"
        activeProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-white"
        }}
        inactiveProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        }}
      >
        <TrendingUp className="inline w-4 h-4 mr-2" />
        Project Metrics
      </Link>

        <div className="flex-shrink-0"> | </div>

      <Link
        to="/pipeline-reports"
        activeProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-white"
        }}
        inactiveProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        }}
      >
        <FileBarChart2 className="inline w-4 h-4 mr-2" />
        Pipeline Reports
      </Link>
      <Link
        to="/recommendation-reports"
        activeProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-accent-emphasis border border-gh-accent-primary rounded-md hover:bg-gh-accent-primary transition-colors text-white"
        }}
        inactiveProps={{
          className: "px-3 py-1.5 text-xs md:px-4 md:py-2 md:text-sm whitespace-nowrap bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        }}
      >
        <ClipboardList className="inline w-4 h-4 mr-2" />
        Recommendation Reports
      </Link>
    </div>
  )
}
