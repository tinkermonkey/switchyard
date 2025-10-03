import { useSocket } from '../contexts/SocketContext'

export default function Header() {
  const { connected } = useSocket()

  return (
    <div className="bg-gh-canvas-subtle p-5 rounded-md mb-5 border border-gh-border">
      <h1 className="text-gh-accent-primary text-2xl font-semibold mb-3">
        Agent Observability Dashboard
      </h1>
      <span className={`inline-block px-3 py-1 rounded-full text-xs font-semibold ${
        connected
          ? 'bg-gh-success text-white'
          : 'bg-gh-danger text-white'
      }`}>
        {connected ? 'Connected' : 'Disconnected'}
      </span>
    </div>
  )
}
