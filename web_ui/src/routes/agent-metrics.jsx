import { createFileRoute } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import AgentMetrics from '../components/AgentMetrics'

function AgentMetricsPage() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <AgentMetrics />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/agent-metrics')({
  component: AgentMetricsPage,
})
