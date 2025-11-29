import { useEffect, useState } from 'react'
import { AlertCircle, TrendingUp, CheckCircle, XCircle } from 'lucide-react'
import { useSocket } from '../contexts'

export default function MedicDashboard() {
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
      fetchStats()
    }
  }, [medicEvents])

  const fetchStats = async () => {
    try {
      const response = await fetch('/api/medic/stats')
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
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        title="Total Signatures"
        value={stats?.total_signatures || 0}
        icon={<AlertCircle className="w-5 h-5" />}
        color="blue"
      />
      <StatCard
        title="New"
        value={stats?.by_status?.new || 0}
        icon={<AlertCircle className="w-5 h-5" />}
        color="yellow"
      />
      <StatCard
        title="Trending"
        value={stats?.by_status?.trending || 0}
        icon={<TrendingUp className="w-5 h-5" />}
        color="orange"
      />
      <StatCard
        title="Resolved"
        value={stats?.by_status?.resolved || 0}
        icon={<CheckCircle className="w-5 h-5" />}
        color="green"
      />

      <StatCard
        title="Critical Errors"
        value={stats?.by_severity?.CRITICAL || 0}
        icon={<XCircle className="w-5 h-5" />}
        color="red"
      />
      <StatCard
        title="Errors"
        value={stats?.by_severity?.ERROR || 0}
        icon={<AlertCircle className="w-5 h-5" />}
        color="orange"
      />
      <StatCard
        title="Warnings"
        value={stats?.by_severity?.WARNING || 0}
        icon={<AlertCircle className="w-5 h-5" />}
        color="yellow"
      />
      <StatCard
        title="Total Occurrences"
        value={stats?.total_occurrences || 0}
        icon={<TrendingUp className="w-5 h-5" />}
        color="blue"
      />
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
