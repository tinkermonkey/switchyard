import { createFileRoute, useNavigate } from '@tanstack/react-router'
import CycleMetrics from '../../components/CycleMetrics'

function CycleMetricsPage() {
  const { days } = Route.useSearch()
  const navigate = useNavigate({ from: '/metrics/cycle' })

  const handleDaysChange = (d) => {
    navigate({ to: '/metrics/cycle', search: { days: d } })
  }

  return <CycleMetrics days={days} onDaysChange={handleDaysChange} />
}

export const Route = createFileRoute('/metrics/cycle')({
  validateSearch: (search) => ({
    days: Number(search?.days) || 7,
  }),
  component: CycleMetricsPage,
})
