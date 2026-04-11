import { createFileRoute, useNavigate } from '@tanstack/react-router'
import ProjectMetrics from '../../components/ProjectMetrics'

function ProjectMetricsPage() {
  const { days } = Route.useSearch()
  const navigate = useNavigate({ from: '/metrics/project' })

  const handleDaysChange = (d) => {
    navigate({ to: '/metrics/project', search: { days: d } })
  }

  return <ProjectMetrics days={days} onDaysChange={handleDaysChange} />
}

export const Route = createFileRoute('/metrics/project')({
  validateSearch: (search) => ({
    days: Number(search?.days) || 7,
  }),
  component: ProjectMetricsPage,
})
