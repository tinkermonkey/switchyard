import { useState } from 'react'
import Header from './Header'
import NavigationTabs from './NavigationTabs'
import ClaudeMedicDashboard from './claude-medic/ClaudeMedicDashboard'
import ClaudeFailureSignatureList from './claude-medic/ClaudeFailureSignatureList'

export default function ClaudeMedic() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />

      <div className="mt-6 space-y-6">
        {/* Page Header */}
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg px-6 py-4">
          <h2 className="text-xl font-semibold text-gh-fg">Claude Medic - Tool Execution Monitor</h2>
          <p className="text-sm text-gh-fg-muted mt-1">
            Monitor and investigate Claude Code tool execution failures across all projects
          </p>
        </div>

        {/* Dashboard Stats */}
        <div>
          <h3 className="text-lg font-semibold text-gh-fg mb-3">Overview</h3>
          <ClaudeMedicDashboard />
        </div>

        {/* Failure Signatures */}
        <div>
          <h3 className="text-lg font-semibold text-gh-fg mb-3">Failure Signatures</h3>
          <ClaudeFailureSignatureList />
        </div>
      </div>
    </div>
  )
}
