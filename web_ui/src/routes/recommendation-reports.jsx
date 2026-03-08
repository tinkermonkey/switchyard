import { createFileRoute, useNavigate } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import RecommendationReports from '../components/RecommendationReports'

function RecommendationReportsPage() {
  const search = Route.useSearch()
  const navigate = useNavigate({ from: '/recommendation-reports' })

  const handleSearchChange = (updates) => {
    navigate({ to: '/recommendation-reports', search: (prev) => ({ ...prev, ...updates }) })
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <RecommendationReports search={search} onSearchChange={handleSearchChange} />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/recommendation-reports')({
  validateSearch: (search) => ({
    project: typeof search.project === 'string' ? search.project : '',
    recType: ['all', 'orchestrator', 'project'].includes(search.recType) ? search.recType : 'all',
  }),
  component: RecommendationReportsPage,
})
