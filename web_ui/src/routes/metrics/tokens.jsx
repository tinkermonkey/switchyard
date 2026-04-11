import { createFileRoute, useNavigate } from '@tanstack/react-router'
import TokenMetrics from '../../components/TokenMetrics'

function TokenMetricsPage() {
  const { days } = Route.useSearch()
  const navigate = useNavigate({ from: '/metrics/tokens' })

  const handleDaysChange = (d) => {
    navigate({ to: '/metrics/tokens', search: { days: d } })
  }

  return <TokenMetrics days={days} onDaysChange={handleDaysChange} />
}

export const Route = createFileRoute('/metrics/tokens')({
  validateSearch: (search) => ({
    days: Number(search?.days) || 7,
  }),
  component: TokenMetricsPage,
})
