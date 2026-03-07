import { createFileRoute, useNavigate } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import ProjectMetrics from '../components/ProjectMetrics'

function ProjectMetricsPage() {
  const { days } = Route.useSearch()
  const navigate = useNavigate({ from: '/project-metrics' })

  const handleDaysChange = (d) => {
    navigate({ to: '/project-metrics', search: { days: d } })
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <ProjectMetrics days={days} onDaysChange={handleDaysChange} />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/project-metrics')({
  validateSearch: (search) => ({
    days: Number(search?.days) || 7,
  }),
  component: ProjectMetricsPage,
})
