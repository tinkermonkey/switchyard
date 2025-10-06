import { createRootRoute, Outlet } from '@tanstack/react-router'
import { ThemeProvider } from '../contexts/ThemeContext'
import { SocketProvider } from '../contexts/SocketContext'

export const Route = createRootRoute({
  component: () => (
    <ThemeProvider>
      <SocketProvider>
        <div className="min-h-screen bg-gh-canvas text-gh-fg">
          <Outlet />
        </div>
      </SocketProvider>
    </ThemeProvider>
  ),
})
