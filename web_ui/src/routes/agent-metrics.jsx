import { createFileRoute, useNavigate } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import AgentMetrics from '../components/AgentMetrics'

function AgentMetricsPage() {
  const { days } = Route.useSearch()
  const navigate = useNavigate({ from: '/agent-metrics' })

  const handleDaysChange = (d) => {
    navigate({ to: '/agent-metrics', search: { days: d } })
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <AgentMetrics days={days} onDaysChange={handleDaysChange} />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/agent-metrics')({
  validateSearch: (search) => ({
    days: Number(search?.days) || 7,
  }),
  component: AgentMetricsPage,
})
