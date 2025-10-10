import Header from './Header'
import AgentState from './AgentState'
import LiveLogs from './LiveLogs'
import EventTimeline from './EventTimeline'
import NavigationTabs from './NavigationTabs'

export default function Dashboard() {
  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />
      <AgentState />
      <LiveLogs />
      <EventTimeline />
    </div>
  )
}
