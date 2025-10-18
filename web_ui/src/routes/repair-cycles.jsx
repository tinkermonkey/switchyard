import { createFileRoute } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import RepairCycleContainers from '../components/RepairCycleContainers'

function RepairCycles() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="mt-6">
        <RepairCycleContainers />
      </div>
    </div>
  )
}

export const Route = createFileRoute('/repair-cycles')({
  component: RepairCycles,
})
