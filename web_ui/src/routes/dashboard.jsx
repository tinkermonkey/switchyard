import { createFileRoute } from '@tanstack/react-router'
import { useState, useEffect, useCallback } from 'react'
import { Link } from '@tanstack/react-router'
import { RefreshCw } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import DashboardRunGraph from '../components/DashboardRunGraph'
import { mergeArrayByIdStable } from '../utils/eventMerging'

const MAX_DISPLAYED = 9

function getGridLayout(count) {
  if (count <= 1) return { cols: 1, rows: 1 }
  if (count <= 2) return { cols: 2, rows: 1 }
  if (count <= 4) return { cols: 2, rows: 2 }
  if (count <= 6) return { cols: 3, rows: 2 }
  return { cols: 3, rows: 3 }
}

function DashboardView() {
  const [activeRuns, setActiveRuns] = useState([])
  const [loading, setLoading] = useState(true)

  const fetchActiveRuns = useCallback(async (isInitial = false) => {
    try {
      if (isInitial) setLoading(true)
      const response = await fetch('/active-pipeline-runs')
      const data = await response.json()
      if (data.success) {
        setActiveRuns(prev => mergeArrayByIdStable(prev, data.runs))
      }
    } catch (error) {
      console.error('[Dashboard] Error fetching active runs:', error)
    } finally {
      if (isInitial) setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchActiveRuns(true)
    const id = setInterval(() => fetchActiveRuns(), 10000)
    return () => clearInterval(id)
  }, [fetchActiveRuns])

  const overflowCount = Math.max(0, activeRuns.length - MAX_DISPLAYED)
  // Reserve one cell for the overflow card so it fits within the declared grid
  const displayedRuns = activeRuns.slice(0, overflowCount > 0 ? MAX_DISPLAYED - 1 : MAX_DISPLAYED)
  const totalCells = displayedRuns.length + (overflowCount > 0 ? 1 : 0)
  const { cols, rows } = getGridLayout(totalCells)

  return (
    <div className="h-screen flex flex-col p-2 md:p-5 bg-gh-canvas text-gh-fg">
      <Header />

      <NavigationTabs />

      <div className="flex-1 min-h-0">
        {loading && activeRuns.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <RefreshCw className="w-8 h-8 animate-spin text-gh-accent-primary" />
          </div>
        ) : activeRuns.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <p className="text-gh-fg-muted">No active pipeline runs</p>
          </div>
        ) : (
          <div
            className="h-full gap-2"
            style={{
              display: 'grid',
              gridTemplateColumns: `repeat(${cols}, 1fr)`,
              gridTemplateRows: `repeat(${rows}, 1fr)`,
            }}
          >
            {displayedRuns.map(run => (
              <DashboardRunGraph key={run.id} run={run} />
            ))}
            {overflowCount > 0 && (
              <Link
                to="/pipeline-run"
                className="flex items-center justify-center bg-gh-canvas-subtle border border-gh-border rounded-md text-gh-fg-muted hover:border-gh-accent-primary transition-colors"
              >
                +{activeRuns.length - displayedRuns.length} more run{activeRuns.length - displayedRuns.length !== 1 ? 's' : ''}
              </Link>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export const Route = createFileRoute('/dashboard')({
  component: DashboardView,
})
