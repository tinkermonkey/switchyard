import { createFileRoute, useNavigate } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import PipelineReports from '../components/PipelineReports'

function PipelineReportsPage() {
  const search = Route.useSearch()
  const navigate = useNavigate({ from: '/pipeline-reports' })

  const handleSearchChange = (updates) => {
    navigate({ to: '/pipeline-reports', search: (prev) => ({ ...prev, ...updates }) })
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <PipelineReports search={search} onSearchChange={handleSearchChange} />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/pipeline-reports')({
  validateSearch: (search) => ({
    project: typeof search.project === 'string' ? search.project : '',
    board: typeof search.board === 'string' ? search.board : '',
    outcome: typeof search.outcome === 'string' ? search.outcome : '',
    page: Number(search.page) || 0,
    sortCol: typeof search.sortCol === 'string' ? search.sortCol : 'started_at',
    sortDir: search.sortDir === 'asc' ? 'asc' : 'desc',
  }),
  component: PipelineReportsPage,
})
