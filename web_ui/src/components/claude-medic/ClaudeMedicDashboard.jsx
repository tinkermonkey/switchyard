import { useEffect, useState } from 'react'
import { AlertCircle, TrendingUp, CheckCircle, FolderGit2, Wrench, Code2 } from 'lucide-react'
import { useSocket } from '../../contexts'

export default function ClaudeMedicDashboard() {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const { medicEvents } = useSocket()

  useEffect(() => {
    fetchStats()
    const interval = setInterval(fetchStats, 30000) // Refresh every 30 seconds
    return () => clearInterval(interval)
  }, [])

  // Refresh stats when medic events occur
  useEffect(() => {
    if (medicEvents.length > 0) {
      const lastEvent = medicEvents[0]
      // Only refresh for Claude Medic events
      if (lastEvent.event_type?.includes('claude') || lastEvent.type === 'claude_failure') {
        fetchStats()
      }
    }
  }, [medicEvents])

  const fetchStats = async () => {
    try {
      const response = await fetch('/api/medic/claude/stats')
      if (!response.ok) throw new Error('Failed to fetch stats')
      const data = await response.json()
      setStats(data)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
        <p className="text-sm text-red-500">Error loading stats: {error}</p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Main Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          title="Active Failures"
          value={stats?.active_failures || 0}
          icon={<AlertCircle className="w-5 h-5" />}
          color="red"
        />
        <StatCard
          title="Under Investigation"
          value={stats?.under_investigation || 0}
          icon={<Wrench className="w-5 h-5" />}
          color="blue"
        />
        <StatCard
          title="Resolved This Week"
          value={stats?.resolved_this_week || 0}
          icon={<CheckCircle className="w-5 h-5" />}
          color="green"
        />
        <StatCard
          title="Total Clusters"
          value={stats?.total_clusters || 0}
          icon={<TrendingUp className="w-5 h-5" />}
          color="purple"
        />
      </div>

      {/* Top Failing Projects */}
      {stats?.top_projects && stats.top_projects.length > 0 && (
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
          <h4 className="text-sm font-semibold text-gh-fg mb-3 flex items-center gap-2">
            <FolderGit2 className="w-4 h-4" />
            Top Failing Projects
          </h4>
          <div className="space-y-2">
            {stats.top_projects.map((project, idx) => (
              <div key={idx} className="flex items-center justify-between text-sm">
                <span className="text-gh-fg">{project.name}</span>
                <span className="text-gh-fg-muted">{project.failure_count} failures</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* By Tool Type */}
      {stats?.by_tool && Object.keys(stats.by_tool).length > 0 && (
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
          <h4 className="text-sm font-semibold text-gh-fg mb-3 flex items-center gap-2">
            <Code2 className="w-4 h-4" />
            Failures by Tool
          </h4>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Object.entries(stats.by_tool).map(([tool, count]) => (
              <div key={tool} className="bg-gh-canvas border border-gh-border rounded p-2">
                <p className="text-xs text-gh-fg-muted">{tool}</p>
                <p className="text-lg font-bold text-gh-fg">{count}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function StatCard({ title, value, icon, color }) {
  const colorClasses = {
    blue: 'text-blue-500 bg-blue-500/10 border-blue-500/20',
    green: 'text-green-500 bg-green-500/10 border-green-500/20',
    yellow: 'text-yellow-500 bg-yellow-500/10 border-yellow-500/20',
    orange: 'text-orange-500 bg-orange-500/10 border-orange-500/20',
    red: 'text-red-500 bg-red-500/10 border-red-500/20',
    purple: 'text-purple-500 bg-purple-500/10 border-purple-500/20',
  }

  return (
    <div className={`p-4 rounded-lg border ${colorClasses[color]}`}>
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-gh-fg-muted">{title}</p>
          <p className="text-2xl font-bold mt-1">{value}</p>
        </div>
        <div className={`${colorClasses[color]}`}>
          {icon}
        </div>
      </div>
    </div>
  )
}
