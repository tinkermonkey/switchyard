/**
 * AppStateProvider - Root provider that composes all state contexts
 * 
 * Order matters:
 * 1. SocketContext - Real-time events (foundation for AgentStateContext)
 * 2. SystemStateContext - Health & circuit breakers
 * 3. ProjectStateContext - Project data
 * 4. AgentStateContext - Agent state (depends on SocketContext)
 */
import { SocketProvider } from './SocketContext'
import { ThemeProvider } from './ThemeContext'
import { SystemStateProvider } from './SystemStateContext'
import { ProjectStateProvider } from './ProjectStateContext'
import { AgentStateProvider } from './AgentStateContext'

export function AppStateProvider({ children }) {
  return (
    <ThemeProvider>
      <SocketProvider>
        <SystemStateProvider>
          <ProjectStateProvider>
            <AgentStateProvider>
              {children}
            </AgentStateProvider>
          </ProjectStateProvider>
        </SystemStateProvider>
      </SocketProvider>
    </ThemeProvider>
  )
}
