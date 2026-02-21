import { createFileRoute } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import CycleMetrics from '../components/CycleMetrics'

function CycleMetricsPage() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <CycleMetrics />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/cycle-metrics')({
  component: CycleMetricsPage,
})
