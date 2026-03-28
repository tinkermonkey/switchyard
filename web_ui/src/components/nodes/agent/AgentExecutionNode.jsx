import { useState } from 'react'
import { Handle, Position } from '@xyflow/react'
import { Activity, CircleCheck, CircleX, OctagonX, Timer, Zap } from 'lucide-react'
import { useTheme } from '../../../contexts'
import AgentExecutionDetailModal from '../../AgentExecutionDetailModal'

const STATUS_THEME_DARK = {
  running:     { bg: '#111d2e', border: '#58a6ff', headerBg: '#0d1f35', color: '#cae8ff', badgeBg: '#58a6ff20', badgeColor: '#58a6ff', statColor: '#adc8e8', iconColor: '#8b949e', pillBg: '#1a3050', pillColor: '#8bb8e8', stripeBg: '#1a3060', runShadow: '0 0 10px rgba(88,166,255,0.4)' },
  completed:   { bg: '#0f1f0f', border: '#2ea043', headerBg: '#0b1e0b', color: '#b0f0b0', badgeBg: '#3fb95020', badgeColor: '#3fb950', statColor: '#9bc49b', iconColor: '#8b949e', pillBg: '#132813', pillColor: '#7ac97a' },
  failed:      { bg: '#1e0a0a', border: '#f85149', headerBg: '#1a0808', color: '#ffd2d2', badgeBg: '#f8514920', badgeColor: '#f85149', statColor: '#d9908e', iconColor: '#8b949e', pillBg: '#2a1010', pillColor: '#e8a0a0' },
  interrupted: { bg: '#1c1208', border: '#8b5e3c', headerBg: '#180f05', color: '#f0d5b8', badgeBg: '#d2905520', badgeColor: '#d29055', statColor: '#7a6050', iconColor: '#8b949e', pillBg: '#221508', pillColor: '#c49070' },
  default:     { bg: '#161b22', border: '#30363d', headerBg: '#0d1117', color: '#c9d1d9', badgeBg: '#30363d',   badgeColor: '#6e7681', statColor: '#6e7681', iconColor: '#8b949e', pillBg: '#21262d', pillColor: '#8b949e' },
}

const STATUS_THEME_LIGHT = {
  running:     { bg: '#e8f4ff', border: '#0969da', headerBg: '#dbeeff', color: '#0550ae', badgeBg: '#d8eaf8',   badgeColor: '#0969da', statColor: '#0969da', iconColor: '#57606a', pillBg: '#cfe2f8', pillColor: '#0550ae', stripeBg: '#3b8fe8', runShadow: '0 0 8px rgba(9,105,218,0.25)' },
  completed:   { bg: '#f0fff4', border: '#2da44e', headerBg: '#e6ffed', color: '#1a7f37', badgeBg: '#d4f0db',   badgeColor: '#2da44e', statColor: '#1a7f37', iconColor: '#57606a', pillBg: '#dcffe4', pillColor: '#1a7f37' },
  failed:      { bg: '#fff0f0', border: '#cf222e', headerBg: '#ffebe9', color: '#82071e', badgeBg: '#fac0c0',   badgeColor: '#cf222e', statColor: '#cf222e', iconColor: '#57606a', pillBg: '#ffd7d5', pillColor: '#82071e' },
  interrupted: { bg: '#fff8f0', border: '#bc4c00', headerBg: '#fff1e4', color: '#7d3200', badgeBg: '#f5dab8',   badgeColor: '#bc4c00', statColor: '#bc4c00', iconColor: '#57606a', pillBg: '#ffe8d0', pillColor: '#7d3200' },
  default:     { bg: '#f6f8fa', border: '#d0d7de', headerBg: '#eaeef2', color: '#24292f', badgeBg: '#d0d7de',   badgeColor: '#57606a', statColor: '#57606a', iconColor: '#57606a', pillBg: '#e0e6eb', pillColor: '#57606a' },
}

const STATUS_ICON = {
  running:     <Activity size={14} />,
  completed:   <CircleCheck size={14} />,
  failed:      <CircleX size={14} />,
  interrupted: <OctagonX size={14} />,
}

const STATUS_LABEL = {
  running:     'running',
  completed:   'done',
  failed:      'failed',
  interrupted: 'killed',
  default:     'done',
}

function formatDuration(ms) {
  if (ms == null || ms < 0) return null
  const totalSec = Math.round(ms / 1000)
  if (totalSec < 60) return `${totalSec}s`
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return s > 0 ? `${m}m ${s}s` : `${m}m`
}

function formatTokens(n) {
  if (n == null) return null
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

const KNOWN_STATUSES = new Set(['running', 'completed', 'failed', 'interrupted', undefined])

export default function AgentExecutionNode({ data }) {
  if (!data) return null
  const { status, isActive, label, durationMs, inputTokens, outputTokens, tools } = data
  const { theme: colorMode } = useTheme()
  const [modalOpen, setModalOpen] = useState(false)
  const executionId = data.event?.agent_execution_id || data.event?.data?.agent_execution_id

  if (process.env.NODE_ENV !== 'production' && status !== undefined && !KNOWN_STATUSES.has(status)) {
    console.warn(`AgentExecutionNode: unrecognised status "${status}" — add a style mapping or update KNOWN_STATUSES`)
  }

  // Interrupted wins over isActive (zombie agent detection)
  const effectiveStatus = status === 'interrupted' ? 'interrupted'
    : (isActive || status === 'running') ? 'running'
    : (status ?? 'default')
  const STATUS_THEME = colorMode === 'light' ? STATUS_THEME_LIGHT : STATUS_THEME_DARK
  const theme = STATUS_THEME[effectiveStatus] ?? STATUS_THEME.default
  const icon = STATUS_ICON[effectiveStatus] ?? STATUS_ICON.running

  const totalTokens = (inputTokens ?? 0) + (outputTokens ?? 0)
  const durationStr = formatDuration(durationMs)
  const tokensStr = formatTokens(totalTokens > 0 ? totalTokens : null)

  // Show stats row if either stat has data, or if running (always show duration placeholder)
  const hasStats = durationStr || tokensStr || effectiveStatus === 'running'
  const hasTools = tools?.length > 0

  return (
    <>
    <div
      style={{
        minWidth: 220,
        maxWidth: 280,
        borderRadius: 8,
        border: `2px solid ${theme.border}`,
        background: theme.bg,
        color: theme.color,
        boxShadow: effectiveStatus === 'running'
          ? (theme.runShadow ?? '0 0 10px rgba(88,166,255,0.4)')
          : '0 2px 6px rgba(0,0,0,0.15)',
        overflow: 'hidden',
        fontSize: 12,
        ...(executionId && { cursor: 'pointer' }),
      }}
      onClick={executionId ? (e) => { e.stopPropagation(); setModalOpen(true) } : undefined}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <Handle id="left"  type="target" position={Position.Left}  style={{ opacity: 0 }} />
      <Handle id="right" type="source" position={Position.Right} style={{ opacity: 0 }} />

      {/* Running stripe animation */}
      {effectiveStatus === 'running' && (
        <div
          style={{
            height: 3,
            background: theme.stripeBg ?? theme.border,
            backgroundImage:
              'linear-gradient(45deg, rgba(255,255,255,.25) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.25) 50%, rgba(255,255,255,.25) 75%, transparent 75%, transparent)',
            backgroundSize: '1rem 1rem',
            animation: 'stripes 1s linear infinite',
          }}
        />
      )}

      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 7,
          padding: '0 10px',
          height: 36,
          background: theme.headerBg,
          borderBottom: hasStats || hasTools ? `1px solid ${theme.border}33` : 'none',
          flexShrink: 0,
        }}
      >
        <span style={{ flexShrink: 0, display: 'flex', opacity: 0.9 }}>{icon}</span>
        <span style={{
          fontWeight: 600,
          fontSize: 12,
          flex: 1,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>
          {label}
        </span>
        {/* Status pill badge */}
        <span style={{
          fontSize: 9,
          fontWeight: 600,
          padding: '2px 6px',
          borderRadius: 10,
          background: theme.badgeBg,
          color: theme.badgeColor,
          flexShrink: 0,
          whiteSpace: 'nowrap',
        }}>
          {STATUS_LABEL[effectiveStatus] ?? effectiveStatus}
        </span>
      </div>

      {/* Stats row */}
      {hasStats && (
        <div style={{
          display: 'flex',
          gap: 14,
          padding: '7px 10px',
          alignItems: 'center',
          borderBottom: hasTools ? `1px solid ${theme.border}22` : 'none',
        }}>
          {/* Duration — always shown for running (as —), shown when available otherwise */}
          {(durationStr || effectiveStatus === 'running') && (
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <Timer size={12} color={theme.iconColor} style={{ flexShrink: 0 }} />
              <span style={{ color: durationStr ? theme.statColor : theme.iconColor }}>
                {durationStr ?? '—'}
              </span>
            </span>
          )}
          {tokensStr && (
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <Zap size={12} color={theme.iconColor} style={{ flexShrink: 0 }} />
              <span style={{ color: theme.statColor }}>{tokensStr}</span>
            </span>
          )}
        </div>
      )}

      {/* Tool pills */}
      {hasTools && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, padding: '6px 10px 8px' }}>
          {tools.map(tool => (
            <span
              key={tool}
              style={{
                fontSize: 10,
                padding: '2px 6px',
                borderRadius: 4,
                background: theme.pillBg,
                color: theme.pillColor,
                fontFamily: 'monospace',
              }}
            >
              {tool}
            </span>
          ))}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
      <Handle id="bottom" type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
    {modalOpen && (
      <AgentExecutionDetailModal executionId={executionId} onClose={() => setModalOpen(false)} />
    )}
    </>
  )
}
