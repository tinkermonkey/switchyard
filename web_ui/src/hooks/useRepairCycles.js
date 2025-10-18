import { useState, useEffect } from 'react'
import { useSocket } from '../contexts/SocketContext'

/**
 * Hook for managing repair cycle container state
 */
export const useRepairCycles = () => {
  const { events } = useSocket()
  const [containers, setContainers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Fetch containers from API
  const fetchContainers = async () => {
    try {
      const response = await fetch('http://localhost:5001/api/repair-cycle-containers')
      const data = await response.json()
      
      if (data.success) {
        setContainers(data.containers)
        setError(null)
      } else {
        setError(data.error || 'Failed to fetch containers')
      }
    } catch (err) {
      setError(`Error fetching containers: ${err.message}`)
    } finally {
      setLoading(false)
    }
  }

  // Fetch on mount and when events change
  useEffect(() => {
    fetchContainers()
    
    // Refresh every 5 seconds
    const interval = setInterval(fetchContainers, 5000)
    
    return () => clearInterval(interval)
  }, [])

  // Listen for repair cycle events
  useEffect(() => {
    if (!events || events.length === 0) return

    const latestEvent = events[events.length - 1]
    
    if (latestEvent.event_type?.startsWith('repair_cycle_container_')) {
      // Refresh when any repair cycle event occurs
      fetchContainers()
    }
  }, [events])

  return {
    containers,
    containerCount: containers.length,
    loading,
    error,
    refetch: fetchContainers
  }
}
