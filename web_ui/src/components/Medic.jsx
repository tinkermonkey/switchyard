import { useState } from 'react'
import Header from './Header'
import NavigationTabs from './NavigationTabs'
import MedicDashboard from './MedicDashboard'
import ActiveInvestigations from './ActiveInvestigations'
import ActiveFixes from './ActiveFixes'
import FailureSignatureList from './FailureSignatureList'

export default function Medic() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />

      <div className="mt-6 space-y-6">
        {/* Page Header */}
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg px-6 py-4">
          <h2 className="text-xl font-semibold text-gh-fg">Medic - System Health Monitor</h2>
          <p className="text-sm text-gh-fg-muted mt-1">
            Monitor and investigate error patterns across Clauditoreum containers
          </p>
        </div>

        {/* Dashboard Stats */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-lg font-semibold text-gh-fg">Overview</h3>
            <p className="text-xs text-gh-fg-muted">Stats reflect all signatures (list below shows filtered/limited view)</p>
          </div>
          <MedicDashboard />
        </div>

        {/* Active Investigations and Fixes Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div>
            <h3 className="text-lg font-semibold text-gh-fg mb-3">Active Investigations</h3>
            <ActiveInvestigations />
          </div>
          <div>
            <h3 className="text-lg font-semibold text-gh-fg mb-3">Active Fix Executions</h3>
            <ActiveFixes />
          </div>
        </div>

        {/* Failure Signatures */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-lg font-semibold text-gh-fg">Failure Signatures</h3>
            <p className="text-xs text-gh-fg-muted">Showing up to 50 signatures (use filters to refine)</p>
          </div>
          <FailureSignatureList />
        </div>
      </div>
    </div>
  )
}
