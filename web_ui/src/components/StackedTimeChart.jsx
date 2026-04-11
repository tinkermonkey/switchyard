import { useState } from 'react'

const PALETTE = [
  '#3b82f6', // blue
  '#22c55e', // green
  '#f59e0b', // amber
  '#ef4444', // red
  '#a855f7', // purple
  '#06b6d4', // cyan
  '#f97316', // orange
  '#84cc16', // lime
  '#ec4899', // pink
  '#14b8a6', // teal
]

const segLabel = (ms, days) => {
  const d = new Date(ms)
  if (days === 1) return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (days <= 3) {
    const day = d.toLocaleDateString('en-US', { weekday: 'short' })
    return `${day} ${d.getHours().toString().padStart(2, '0')}:00`
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/**
 * Stacked bar chart for total values over time (not averages).
 *
 * series: { name: [{h: isoString, value: number}] }
 *   value = raw total for that time bucket
 *
 * formatValue(n): formats a number for axis labels and tooltips
 * title: descriptive label shown above the chart
 */
export default function StackedTimeChart({ series, days, formatValue, title }) {
  const [hovered, setHovered] = useState(null)

  const SEGS = 24
  const names = Object.keys(series).sort()
  if (!names.length) return null

  const now = Date.now()
  const winMs = days * 24 * 60 * 60 * 1000
  const segMs = winMs / SEGS
  const winStart = now - winMs

  // Accumulate values per segment per name
  const segs = Array.from({ length: SEGS }, (_, i) => ({
    start: winStart + i * segMs,
    byName: {},
  }))

  for (const [name, docs] of Object.entries(series)) {
    for (const { h, value } of docs) {
      const idx = Math.floor((new Date(h).getTime() - winStart) / segMs)
      if (idx >= 0 && idx < SEGS) {
        const b = segs[idx].byName
        b[name] = (b[name] || 0) + (value || 0)
      }
    }
  }

  const processed = segs.map(seg => {
    const slices = names.map(name => seg.byName[name] || 0)
    const total = slices.reduce((a, b) => a + b, 0)
    return { start: seg.start, slices, total }
  })

  const maxTotal = Math.max(...processed.map(s => s.total), 1)

  const VW = 800
  const VH = 150
  const PAD = { t: 8, r: 4, b: 28, l: 56 }
  const cW = VW - PAD.l - PAD.r
  const cH = VH - PAD.t - PAD.b
  const step = cW / SEGS
  const barW = step * 0.72

  const yTicks = [0, 0.5, 1.0]
  const xLabelIdxs = [0, 4, 8, 12, 16, 20, 23]

  return (
    <div className="bg-gh-canvas-subtle border border-gh-border rounded-md p-3">
      {title && <p className="text-xs text-gh-fg-muted mb-2">{title}</p>}
      <div className="flex gap-3">
        <div className="relative flex-1 min-w-0">
          <svg
            viewBox={`0 0 ${VW} ${VH}`}
            className="w-full"
            style={{ height: '150px' }}
            onMouseLeave={() => setHovered(null)}
          >
            {yTicks.map(f => {
              const y = PAD.t + cH * (1 - f)
              return (
                <g key={f}>
                  <line
                    x1={PAD.l} y1={y} x2={PAD.l + cW} y2={y}
                    stroke="#555" strokeWidth="0.5"
                    strokeDasharray={f > 0 ? '3,3' : ''} opacity="0.4"
                  />
                  <text x={PAD.l - 3} y={y + 3} textAnchor="end" fontSize="9" fill="#888">
                    {formatValue(maxTotal * f)}
                  </text>
                </g>
              )
            })}

            {processed.map((seg, i) => {
              if (seg.total === 0) return null
              let cumulH = 0
              return (
                <g key={i} onMouseEnter={() => setHovered(i)} style={{ cursor: 'crosshair' }}>
                  {names.map((name, ni) => {
                    const val = seg.slices[ni]
                    if (!val) return null
                    const h = (val / maxTotal) * cH
                    const y = PAD.t + cH - cumulH - h
                    cumulH += h
                    return (
                      <rect
                        key={name}
                        x={PAD.l + i * step + (step - barW) / 2}
                        y={y}
                        width={barW}
                        height={h}
                        fill={PALETTE[ni % PALETTE.length]}
                        opacity="0.85"
                      />
                    )
                  })}
                </g>
              )
            })}

            {xLabelIdxs.map(i => (
              <text
                key={i}
                x={PAD.l + i * step + step / 2}
                y={VH - 6}
                textAnchor="middle"
                fontSize="8.5"
                fill="#888"
              >
                {segLabel(processed[i].start, days)}
              </text>
            ))}
          </svg>

          {hovered !== null && processed[hovered].total > 0 && (
            <div className="absolute top-1 right-1 bg-gh-canvas border border-gh-border rounded shadow-md p-2 text-xs pointer-events-none min-w-36 z-10">
              <div className="text-gh-fg-muted mb-1.5 font-medium">
                {segLabel(processed[hovered].start, days)}
              </div>
              {names.map((name, ni) => {
                const val = processed[hovered].slices[ni]
                if (!val) return null
                return (
                  <div key={name} className="flex items-center gap-1.5 py-0.5">
                    <div className="w-2 h-2 rounded-sm flex-shrink-0" style={{ background: PALETTE[ni % PALETTE.length] }} />
                    <span className="text-gh-fg-muted truncate max-w-28">{name}</span>
                    <span className="font-mono ml-auto pl-1">{formatValue(val)}</span>
                  </div>
                )
              })}
              <div className="border-t border-gh-border mt-1 pt-1 flex justify-between">
                <span className="text-gh-fg-muted">Total</span>
                <span className="font-mono">{formatValue(processed[hovered].total)}</span>
              </div>
            </div>
          )}
        </div>

        <div className="flex flex-col gap-1 flex-shrink-0 justify-center">
          {names.map((name, i) => (
            <div key={name} className="flex items-center gap-1.5">
              <div className="w-2.5 h-2.5 rounded-sm flex-shrink-0" style={{ background: PALETTE[i % PALETTE.length] }} />
              <span className="text-xs text-gh-fg-muted font-mono">{name}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
