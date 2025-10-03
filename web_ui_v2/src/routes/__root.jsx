import { createRootRoute, Outlet } from '@tanstack/react-router'
import { TanStackRouterDevtools } from '@tanstack/router-devtools'
import { ThemeProvider } from '../contexts/ThemeContext'
import { SocketProvider } from '../contexts/SocketContext'

export const Route = createRootRoute({
  component: () => (
    <ThemeProvider>
      <SocketProvider>
        <div className="min-h-screen bg-gh-canvas dark:bg-gh-canvas text-gh-fg dark:text-gh-fg">
          <Outlet />
        </div>
        <TanStackRouterDevtools />
      </SocketProvider>
    </ThemeProvider>
  ),
})
