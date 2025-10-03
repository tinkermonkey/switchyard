import { Link } from '@tanstack/react-router'
import { Sun, Moon, Activity, GitBranch } from 'lucide-react'
import { useTheme } from '../contexts/ThemeContext'
import { useSocket } from '../contexts/SocketContext'
import Header from './Header'
import StatsCards from './StatsCards'
import LiveLogs from './LiveLogs'
import EventTimeline from './EventTimeline'

export default function Dashboard() {
  const { theme, toggleTheme } = useTheme()
  const { connected } = useSocket()

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />

      <div className="flex gap-3 mb-5">
        <Link
          to="/"
          className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        >
          <Activity className="inline w-4 h-4 mr-2" />
          Dashboard
        </Link>
        <Link
          to="/pipeline"
          className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm"
        >
          <GitBranch className="inline w-4 h-4 mr-2" />
          Pipeline View
        </Link>
        <button
          onClick={toggleTheme}
          className="ml-auto px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
        >
          {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
        </button>
      </div>

      <StatsCards />
      <LiveLogs />
      <EventTimeline />
    </div>
  )
}
