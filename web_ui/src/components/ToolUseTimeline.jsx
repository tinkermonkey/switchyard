import { useState, useEffect, useMemo, useRef } from 'react'
import { scaleTime, scaleBand, scaleSqrt } from 'd3-scale'
import { max } from 'd3-array'
import { timeFormat } from 'd3-time-format'
import { useSocket } from '../contexts/SocketContext'

const WINDOW_MS = 30_000
const RIGHT_MARGIN_MS = 2_000
const LEFT_PAD = 90
const RIGHT_PAD = 10
const TOP_PAD = 8
const BOTTOM_PAD = 20
const ROW_HEIGHT = 22

const fmtTime = timeFormat('%-M:%S')

function parseToolEvents(rawEvent, ts, idPrefix) {
  if (!rawEvent || rawEvent.type !== 'assistant') return []
  const content = rawEvent.message?.content
  if (!Array.isArray(content)) return []
  const outputTokens = rawEvent.message?.usage?.output_tokens || 0
  return content
    .filter(item => item.type === 'tool_use' && item.name)
    .map((item, i) => {
      const toolName = item.name === 'Task' && item.input?.subagent_type
        ? item.input.subagent_type
        : item.name === 'Skill' && item.input?.skill
          ? item.input.skill
          : item.name
      return { id: `${idPrefix}-${i}`, ts, toolName, outputTokens }
    })
}

/**
 * Rolling 30-second scatter plot of tool calls.
 *
 * Props:
 *   run       - pipeline run object (needs run.id)
 *   allEvents - mergedEvents from useDashboardRunData — reuses the already-fetched
 *               pipeline-run-events data rather than making a duplicate API call.
 *               claude_log entries within this array are parsed for tool_use content.
 */
export default function ToolUseTimeline({ run, allEvents = [] }) {
  const [currentTime, setCurrentTime] = useState(() => new Date())
  const [tooltip, setTooltip] = useState(null)
  const svgRef = useRef(null)
  const { logs } = useSocket()

  // Parse historical tool events from the already-fetched allEvents prop
  const historicalEvents = useMemo(() => {
    const events = []
    for (const evt of allEvents) {
      if (evt.event_category !== 'claude_log') continue
      events.push(...parseToolEvents(
        evt.raw_event?.event,
        new Date(evt.timestamp),
        evt.id || evt.timestamp,
      ))
    }
    return events
  }, [allEvents])

  // Live events from socket, filtered to this run
  const liveEvents = useMemo(() => {
    if (!run?.id || !logs) return []
    const events = []
    for (const log of logs) {
      const matchesRun = log.pipeline_run_id === run.id ||
        (!log.pipeline_run_id && log.task_id === run.id)
      if (!matchesRun) continue
      events.push(...parseToolEvents(
        log.event,
        new Date(log.timestamp),
        `${log.timestamp}-${log.agent}`,
      ))
    }
    return events
  }, [logs, run?.id])

  // Merge historical + live, dedup by id
  const toolEvents = useMemo(() => {
    const seen = new Set()
    const merged = []
    for (const e of [...historicalEvents, ...liveEvents]) {
      if (!seen.has(e.id)) {
        seen.add(e.id)
        merged.push(e)
      }
    }
    return merged
  }, [historicalEvents, liveEvents])

  // Rolling clock — requestAnimationFrame for continuous smooth scrolling
  useEffect(() => {
    let rafId
    const tick = () => {
      setCurrentTime(new Date())
      rafId = requestAnimationFrame(tick)
    }
    rafId = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafId)
  }, [])

  const toolNames = useMemo(() => {
    const names = new Set(toolEvents.map(e => e.toolName))
    return [...names].sort()
  }, [toolEvents])

  const chartHeight = Math.max(60, toolNames.length * ROW_HEIGHT + TOP_PAD + BOTTOM_PAD)

  // D3 scales
  const now = currentTime
  const xDomain = [new Date(now.getTime() - WINDOW_MS - RIGHT_MARGIN_MS), new Date(now.getTime() + RIGHT_MARGIN_MS)]
  const VW = 800
  const xScale = scaleTime().domain(xDomain).range([LEFT_PAD, VW - RIGHT_PAD])
  const yScale = scaleBand().domain(toolNames).range([TOP_PAD, chartHeight - BOTTOM_PAD]).padding(0.3)
  const maxTokens = max(toolEvents, e => e.outputTokens) || 1
  const rScale = scaleSqrt().domain([0, maxTokens]).range([10, 25])

  const xTicks = xScale.ticks(7)
  const nowX = xScale(now)
  const [rangeStart, rangeEnd] = xScale.range()

  const visibleEvents = toolEvents.filter(e => {
    const x = xScale(e.ts)
    return x >= rangeStart && x <= rangeEnd
  })

  const handleDotMouseEnter = (e, event) => {
    const svgRect = svgRef.current?.getBoundingClientRect()
    if (!svgRect) return
    setTooltip({ x: e.clientX - svgRect.left, y: e.clientY - svgRect.top, event })
  }

  if (toolEvents.length === 0) {
    return (
      <div className="px-3 py-1.5 text-xs text-gh-fg-muted italic">
        Waiting for tool calls…
      </div>
    )
  }

  return (
    <div className="relative px-2 py-1" ref={svgRef}>
      <svg
        viewBox={`0 0 ${VW} ${chartHeight}`}
        className="w-full"
        style={{ height: `${chartHeight}px`, display: 'block' }}
        onMouseLeave={() => setTooltip(null)}
      >
        {/* Y-axis row guides + labels */}
        {toolNames.map(name => {
          const y = yScale(name)
          const bw = yScale.bandwidth()
          return (
            <g key={name}>
              <line
                x1={LEFT_PAD} y1={y + bw / 2}
                x2={VW - RIGHT_PAD} y2={y + bw / 2}
                stroke="#555" strokeWidth="0.5" opacity="0.2"
              />
              <text
                x={LEFT_PAD - 6}
                y={y + bw / 2 + 3}
                textAnchor="end"
                fontSize="9"
                fill="#888"
                fontFamily="monospace"
              >
                {name.length > 11 ? `${name.slice(0, 10)}…` : name}
              </text>
            </g>
          )
        })}

        {/* "Now" marker */}
        <line
          x1={nowX} y1={TOP_PAD}
          x2={nowX} y2={chartHeight - BOTTOM_PAD}
          stroke="#4493f8" strokeWidth="1" strokeDasharray="3,3" opacity="0.45"
        />

        {/* X-axis baseline */}
        <line
          x1={LEFT_PAD} y1={chartHeight - BOTTOM_PAD}
          x2={VW - RIGHT_PAD} y2={chartHeight - BOTTOM_PAD}
          stroke="#555" strokeWidth="0.5" opacity="0.3"
        />

        {/* X-axis ticks + time labels */}
        {xTicks.map((t, i) => {
          const x = xScale(t)
          return (
            <g key={i}>
              <line
                x1={x} y1={chartHeight - BOTTOM_PAD}
                x2={x} y2={chartHeight - BOTTOM_PAD + 3}
                stroke="#666" strokeWidth="0.5"
              />
              <text
                x={x} y={chartHeight - BOTTOM_PAD + 11}
                textAnchor="middle" fontSize="8" fill="#666"
              >
                {fmtTime(t)}
              </text>
            </g>
          )
        })}

        {/* Tool call dots */}
        {visibleEvents.map(event => {
          const x = xScale(event.ts)
          const yBand = yScale(event.toolName)
          if (yBand === undefined) return null
          return (
            <circle
              key={event.id}
              cx={x}
              cy={yBand + yScale.bandwidth() / 2}
              r={rScale(event.outputTokens)}
              fill="#4493f8"
              opacity="0.75"
              style={{ cursor: 'crosshair' }}
              onMouseEnter={e => handleDotMouseEnter(e, event)}
              onMouseLeave={() => setTooltip(null)}
            />
          )
        })}
      </svg>

      {tooltip && (
        <div
          className="absolute pointer-events-none z-10 bg-gh-canvas border border-gh-border rounded shadow-md px-2 py-1.5 text-xs"
          style={{ left: tooltip.x + 10, top: Math.max(0, tooltip.y - 10) }}
        >
          <div className="font-mono font-semibold text-gh-fg">{tooltip.event.toolName}</div>
          <div className="text-gh-fg-muted">{fmtTime(tooltip.event.ts)}</div>
          {tooltip.event.outputTokens > 0 && (
            <div className="text-gh-fg-muted">{tooltip.event.outputTokens} output tok</div>
          )}
        </div>
      )}
    </div>
  )
}
