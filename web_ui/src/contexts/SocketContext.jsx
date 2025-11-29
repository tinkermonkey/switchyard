import { createContext, useContext, useEffect, useState } from 'react'
import { io } from 'socket.io-client'

const SocketContext = createContext()

export function SocketProvider({ children }) {
  const [socket, setSocket] = useState(null)
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState([])
  const [logs, setLogs] = useState([])
  const [medicEvents, setMedicEvents] = useState([])
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
    // Load history immediately on mount (don't wait for socket)
    loadHistoryAndStats()

    // In development, Vite proxies /socket.io to the observability server
    // In production, nginx handles the proxy
    const socketInstance = io({
      path: '/socket.io',
      transports: ['websocket', 'polling'],
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      timeout: 3000
    })

    socketInstance.on('connect', () => {
      console.log('Socket connected')
      setConnected(true)
      // Refresh data on reconnect
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
      /*
      console.log('[SocketContext] Received claude_stream_event:', {
        agent: data.agent,
        timestamp: data.timestamp,
        hasEvent: !!data.event
      })
      */
      setLogs(prev => [...prev, data].slice(-200))
    })

    // Medic event listeners
    socketInstance.on('medic_signature_created', (event) => {
      console.log('[SocketContext] Received medic_signature_created:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'signature_created' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_signature_updated', (event) => {
      console.log('[SocketContext] Received medic_signature_updated:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'signature_updated' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_signature_trending', (event) => {
      console.log('[SocketContext] Received medic_signature_trending:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'signature_trending' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_signature_resolved', (event) => {
      console.log('[SocketContext] Received medic_signature_resolved:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'signature_resolved' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_investigation_queued', (event) => {
      console.log('[SocketContext] Received medic_investigation_queued:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'investigation_queued' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_investigation_started', (event) => {
      console.log('[SocketContext] Received medic_investigation_started:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'investigation_started' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_investigation_completed', (event) => {
      console.log('[SocketContext] Received medic_investigation_completed:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'investigation_completed' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_investigation_failed', (event) => {
      console.log('[SocketContext] Received medic_investigation_failed:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'investigation_failed' }, ...prev].slice(0, 100))
    })

    // Claude Medic event listeners
    socketInstance.on('medic_claude_signature_created', (event) => {
      console.log('[SocketContext] Received medic_claude_signature_created:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'claude_signature_created' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_claude_signature_updated', (event) => {
      console.log('[SocketContext] Received medic_claude_signature_updated:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'claude_signature_updated' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_claude_signature_trending', (event) => {
      console.log('[SocketContext] Received medic_claude_signature_trending:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'claude_signature_trending' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_claude_cluster_detected', (event) => {
      console.log('[SocketContext] Received medic_claude_cluster_detected:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'claude_cluster_detected' }, ...prev].slice(0, 100))
    })

    socketInstance.on('medic_claude_investigation_completed', (event) => {
      console.log('[SocketContext] Received medic_claude_investigation_completed:', event)
      setMedicEvents(prev => [{ ...event, event_type: 'claude_investigation_completed' }, ...prev].slice(0, 100))
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
  const clearMedicEvents = () => setMedicEvents([])

  return (
    <SocketContext.Provider value={{
      socket,
      connected,
      events,
      logs,
      medicEvents,
      stats,
      clearEvents,
      clearLogs,
      clearMedicEvents
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
