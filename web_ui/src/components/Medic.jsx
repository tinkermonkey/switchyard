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
          <h3 className="text-lg font-semibold text-gh-fg mb-3">Overview</h3>
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
          <h3 className="text-lg font-semibold text-gh-fg mb-3">Failure Signatures</h3>
          <FailureSignatureList />
        </div>
      </div>
    </div>
  )
}
