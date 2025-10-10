import { createRootRoute, Outlet } from '@tanstack/react-router'
import { AppStateProvider } from '../contexts/AppStateProvider'

export const Route = createRootRoute({
  component: () => (
    <AppStateProvider>
      <div className="min-h-screen bg-gh-canvas text-gh-fg">
        <Outlet />
      </div>
    </AppStateProvider>
  ),
})
