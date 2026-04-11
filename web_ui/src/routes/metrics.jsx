import { createFileRoute, Outlet, Link, redirect, useRouterState } from '@tanstack/react-router'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import { BarChart2, TrendingUp, RefreshCw, Coins } from 'lucide-react'

function MetricsSidebar() {
  const location = useRouterState({ select: (s) => s.location })
  const days = new URLSearchParams(location.search).get('days')
  const search = days ? { days: Number(days) } : {}

  const navItem = ({ to, icon: Icon, label }) => (
    <Link
      to={to}
      search={search}
      activeProps={{
        className: 'flex items-center gap-2 px-3 py-2 rounded-md text-sm bg-gh-accent-emphasis text-white border border-gh-accent-primary'
      }}
      inactiveProps={{
        className: 'flex items-center gap-2 px-3 py-2 rounded-md text-sm text-gh-fg-muted hover:bg-gh-canvas-subtle hover:text-gh-fg transition-colors'
      }}
    >
      <Icon className="w-4 h-4 flex-shrink-0" />
      {label}
    </Link>
  )

  return (
    <aside className="w-48 flex-shrink-0">
      <div className="sticky top-5">
        <p className="text-xs font-semibold text-gh-fg-muted uppercase tracking-wider mb-2 px-3">Metrics</p>
        <nav className="flex flex-col gap-1">
          {navItem({ to: '/metrics/tokens', icon: Coins, label: 'Token Metrics' })}
          {navItem({ to: '/metrics/agent', icon: BarChart2, label: 'Agent Metrics' })}
          {navItem({ to: '/metrics/cycle', icon: RefreshCw, label: 'Cycle Metrics' })}
          {navItem({ to: '/metrics/project', icon: TrendingUp, label: 'Project Metrics' })}
        </nav>
      </div>
    </aside>
  )
}

function MetricsLayout() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <div className="flex gap-6 mt-6">
        <MetricsSidebar />
        <div className="flex-1 min-w-0">
          <Outlet />
        </div>
      </div>
    </div>
  )
}

export const Route = createFileRoute('/metrics')({
  beforeLoad: ({ location }) => {
    if (location.pathname === '/metrics' || location.pathname === '/metrics/') {
      throw redirect({ to: '/metrics/tokens' })
    }
  },
  component: MetricsLayout,
})
