import { createContext, useContext, useEffect, useState } from 'react'
import { io } from 'socket.io-client'

const SocketContext = createContext()

export function SocketProvider({ children }) {
  const [socket, setSocket] = useState(null)
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState([])
  const [logs, setLogs] = useState([])
  const [stats, setStats] = useState({
    totalEvents: 0,
    activeTasks: 0,
    totalTokens: 0,
    avgLatency: 0,
  })

  // Use refs to persist data across renders
  const activeTasksRef = useState(new Set())[0]
  const apiLatenciesRef = useState([])[0]

  // Load history and calculate initial stats
  const loadHistoryAndStats = () => {
    // Load event history
    fetch('/history?count=50')
      .then(res => res.json())
      .then(data => {
        if (data.success && data.events) {
          setEvents(data.events.reverse())
          // Calculate stats from historical events
          calculateStatsFromHistory(data.events)
        }
      })
      .catch(err => console.error('Failed to load history:', err))

    // Load Claude logs history
    fetch('/claude-logs-history?count=100')
      .then(res => res.json())
      .then(data => {
        if (data.success && data.logs) {
          setLogs(data.logs)
        }
      })
      .catch(err => console.error('Failed to load log history:', err))
  }

  const calculateStatsFromHistory = (historicalEvents) => {
    let totalEvents = 0
    let totalTokens = 0
    const latencies = []

    historicalEvents.forEach(event => {
      totalEvents++

      if (event.event_type === 'task_received') {
        activeTasksRef.add(event.task_id)
      } else if (event.event_type === 'agent_completed' || event.event_type === 'agent_failed') {
        activeTasksRef.delete(event.task_id)
      }

      if (event.event_type === 'claude_api_call_completed') {
        totalTokens += event.data?.total_tokens || 0
        const latency = event.data?.duration_ms || 0
        if (latency > 0) {
          latencies.push(latency)
        }
      }
    })

    // Keep only last 10 latencies for average
    const recentLatencies = latencies.slice(-10)
    apiLatenciesRef.length = 0
    apiLatenciesRef.push(...recentLatencies)

    const avgLatency = recentLatencies.length > 0
      ? Math.round(recentLatencies.reduce((a, b) => a + b, 0) / recentLatencies.length)
      : 0

    setStats({
      totalEvents,
      activeTasks: activeTasksRef.size,
      totalTokens,
      avgLatency,
    })
  }

  useEffect(() => {
    // In development, Vite proxies /socket.io to the observability server
    // In production, nginx handles the proxy
    const socketInstance = io({
      path: '/socket.io',
      transports: ['websocket', 'polling']
    })

    socketInstance.on('connect', () => {
      console.log('Socket connected')
      setConnected(true)
      loadHistoryAndStats()
    })

    socketInstance.on('disconnect', () => {
      console.log('Socket disconnected')
      setConnected(false)
    })

    socketInstance.on('agent_event', (event) => {
      setEvents(prev => [event, ...prev].slice(0, 50))
      updateStatsFromEvent(event)
    })

    socketInstance.on('claude_stream_event', (data) => {
      setLogs(prev => [...prev, data].slice(-200))
    })

    setSocket(socketInstance)

    return () => {
      socketInstance.close()
    }
  }, [])

  const updateStatsFromEvent = (event) => {
    setStats(prev => {
      const newStats = { ...prev }
      newStats.totalEvents = prev.totalEvents + 1

      if (event.event_type === 'task_received') {
        activeTasksRef.add(event.task_id)
        newStats.activeTasks = activeTasksRef.size
      } else if (event.event_type === 'agent_completed' || event.event_type === 'agent_failed') {
        activeTasksRef.delete(event.task_id)
        newStats.activeTasks = activeTasksRef.size
      }

      if (event.event_type === 'claude_api_call_completed') {
        const tokens = event.data?.total_tokens || 0
        newStats.totalTokens = prev.totalTokens + tokens

        apiLatenciesRef.push(event.data?.duration_ms || 0)
        if (apiLatenciesRef.length > 10) apiLatenciesRef.shift()
        const avgLatency = apiLatenciesRef.reduce((a, b) => a + b, 0) / apiLatenciesRef.length
        newStats.avgLatency = Math.round(avgLatency)
      }

      return newStats
    })
  }

  const clearEvents = () => setEvents([])
  const clearLogs = () => setLogs([])

  return (
    <SocketContext.Provider value={{
      socket,
      connected,
      events,
      logs,
      stats,
      clearEvents,
      clearLogs
    }}>
      {children}
    </SocketContext.Provider>
  )
}

export function useSocket() {
  const context = useContext(SocketContext)
  if (!context) {
    throw new Error('useSocket must be used within SocketProvider')
  }
  return context
}
