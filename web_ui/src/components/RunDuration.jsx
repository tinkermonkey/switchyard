import { useState, useEffect } from 'react'
import { parseTimestamp } from '../utils/stateHelpers'

function formatSeconds(totalSecs) {
  if (totalSecs <= 0) return '0s'
  const days  = Math.floor(totalSecs / 86400)
  const hours = Math.floor((totalSecs % 86400) / 3600)
  const mins  = Math.floor((totalSecs % 3600) / 60)
  const secs  = totalSecs % 60
  if (days >= 1)  return `${days}d ${hours}h`
  if (hours >= 1) return `${hours}h ${mins}m`
  if (mins >= 1)  return `${mins}m ${secs}s`
  return `${secs}s`
}

/**
 * Displays the elapsed duration between startedAt and endedAt.
 * When endedAt is omitted the display ticks every second, showing a live
 * elapsed time. When endedAt is provided the value is fixed and no interval
 * is created.
 *
 * Props:
 *   startedAt  - ISO timestamp or unix seconds when the run began
 *   endedAt    - ISO timestamp or unix seconds when the run ended; omit for live
 *   className  - optional CSS class applied to the wrapping <span>
 */
export default function RunDuration({ startedAt, endedAt, className }) {
  const isLive = !!startedAt && !endedAt
  const [, setTick] = useState(0)

  useEffect(() => {
    if (!isLive) return
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [isLive])

  if (!startedAt) return null

  const startDate = parseTimestamp(startedAt)
  if (!startDate) return null
  const start = startDate.getTime()

  const endDate = endedAt ? parseTimestamp(endedAt) : null
  const end = endDate ? endDate.getTime() : Date.now()
  const secs = Math.max(0, Math.floor((end - start) / 1000))

  return <span className={className}>{formatSeconds(secs)}</span>
}
