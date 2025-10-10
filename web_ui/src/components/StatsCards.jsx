import { useSocket } from '../contexts/SocketContext'

export default function StatsCards() {
  const { stats } = useSocket()

  const cards = [
    { title: 'Total Events', value: stats.totalEvents, color: 'text-gh-accent-primary' },
    { title: 'Active Tasks', value: stats.activeTasks, color: 'text-gh-accent-primary' },
    { title: 'Total Tokens', value: stats.totalTokens.toLocaleString(), color: 'text-gh-accent-primary' },
    { title: 'Avg API Latency', value: `${stats.avgLatency}ms`, color: 'text-gh-accent-primary' },
  ]

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-5">
      {cards.map((card, idx) => (
        <div key={idx} className="bg-gh-canvas-subtle p-4 rounded-md border border-gh-border">
          <h3 className="text-gh-fg-muted text-xs uppercase mb-2">
            {card.title}
          </h3>
          <div className={`text-2xl font-semibold ${card.color}`}>
            {card.value}
          </div>
        </div>
      ))}
    </div>
  )
}
