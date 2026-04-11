import { createFileRoute, useNavigate } from '@tanstack/react-router'
import AgentMetrics from '../../components/AgentMetrics'

function AgentMetricsPage() {
  const { days } = Route.useSearch()
  const navigate = useNavigate({ from: '/metrics/agent' })

  const handleDaysChange = (d) => {
    navigate({ to: '/metrics/agent', search: { days: d } })
  }

  return <AgentMetrics days={days} onDaysChange={handleDaysChange} />
}

export const Route = createFileRoute('/metrics/agent')({
  validateSearch: (search) => ({
    days: Number(search?.days) || 7,
  }),
  component: AgentMetricsPage,
})
