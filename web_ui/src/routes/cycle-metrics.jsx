import { createFileRoute, useNavigate } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import CycleMetrics from '../components/CycleMetrics'

function CycleMetricsPage() {
  const { days } = Route.useSearch()
  const navigate = useNavigate({ from: '/cycle-metrics' })

  const handleDaysChange = (d) => {
    navigate({ to: '/cycle-metrics', search: { days: d } })
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <CycleMetrics days={days} onDaysChange={handleDaysChange} />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/cycle-metrics')({
  validateSearch: (search) => ({
    days: Number(search?.days) || 7,
  }),
  component: CycleMetricsPage,
})
