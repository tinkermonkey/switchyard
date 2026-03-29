import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useSocket } from '../contexts/SocketContext'
import { mergePipelineRunEvents } from '../utils/eventMerging'
import { shouldIncludePipelineEvent } from '../components/nodes/EVENT_TYPE_MAP.js'

/**
 * Per-run data fetching hook for the dashboard.
 * Fetches events and workflow config, merges with socket events, and polls for updates.
 *
 * @param {object} run - Pipeline run object with id, project, board fields
 * @returns {{ graphEvents: array, mergedEvents: array, workflowConfig: object|null, loading: boolean }}
 */
export function useDashboardRunData(run) {
  const [apiEvents, setApiEvents] = useState([])
  const [workflowConfig, setWorkflowConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const runIdRef = useRef(run?.id)
  const cancelledRef = useRef(false)
  const { events: socketEvents } = useSocket()

  // Initial fetch: events + workflow config in parallel
  useEffect(() => {
    if (!run?.id) return
    runIdRef.current = run.id
    cancelledRef.current = false
    setApiEvents([])
    setWorkflowConfig(null)
    setLoading(true)

    const fetchEvents = async () => {
      try {
        const response = await fetch(`/pipeline-run-events?pipeline_run_id=${run.id}`)
        const data = await response.json()
        if (!cancelledRef.current && data.success && runIdRef.current === run.id) {
          setApiEvents(data.events.map((event, idx) => ({
            ...event,
            _key: event.id || `${event.timestamp}_${idx}`,
          })))
        }
      } catch (error) {
        console.error(`[useDashboardRunData] events fetch error for ${run.id}:`, error)
      } finally {
        if (!cancelledRef.current) setLoading(false)
      }
    }

    const fetchConfig = async () => {
      if (!run.project || !run.board) return
      try {
        const response = await fetch(`/api/workflow-config/${run.project}/${run.board}`)
        const data = await response.json()
        if (!cancelledRef.current && data.success && runIdRef.current === run.id) {
          setWorkflowConfig(data.workflow)
        }
      } catch (error) {
        console.error(`[useDashboardRunData] config fetch error for ${run.id}:`, error)
      }
    }

    fetchEvents()
    fetchConfig()
    return () => { cancelledRef.current = true }
  }, [run?.id, run?.project, run?.board])

  // Poll events every 30s (silent — no loading state change)
  useEffect(() => {
    if (!run?.id) return
    const poll = async () => {
      try {
        const response = await fetch(`/pipeline-run-events?pipeline_run_id=${run.id}`)
        const data = await response.json()
        if (!cancelledRef.current && data.success && runIdRef.current === run.id) {
          setApiEvents(data.events.map((event, idx) => ({
            ...event,
            _key: event.id || `${event.timestamp}_${idx}`,
          })))
        }
      } catch (error) {
        console.error(`[useDashboardRunData] poll error for ${run.id}:`, error)
      }
    }
    const id = setInterval(poll, 30000)
    return () => clearInterval(id)
  }, [run?.id])

  const mergedEvents = useMemo(() => {
    if (!run) return []
    return mergePipelineRunEvents(apiEvents, socketEvents, run)
  }, [apiEvents, socketEvents, run])

  const graphEvents = useMemo(
    () => mergedEvents.filter(shouldIncludePipelineEvent),
    [mergedEvents],
  )

  return { graphEvents, mergedEvents, workflowConfig, loading }
}
