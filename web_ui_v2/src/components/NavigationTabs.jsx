import { Link } from '@tanstack/react-router'
import { Activity, GitBranch, Sparkles, FolderGit2 } from 'lucide-react'

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
    </div>
  )
}
