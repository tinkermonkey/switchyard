import { Handle, Position } from '@xyflow/react'
import { CircleCheck, CircleX, Activity, Timer } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useTheme } from '../../../contexts'

const STATUS_THEME_DARK = {
  running:   { bg: '#111d2e', border: '#58a6ff', headerBg: '#0d1f35', color: '#cae8ff', badgeBg: '#58a6ff20', badgeColor: '#58a6ff', statColor: '#adc8e8', stripeBg: '#1a3060', runShadow: '0 0 10px rgba(88,166,255,0.4)', contentBg: '#0d1a2e' },
  completed: { bg: '#0f1f0f', border: '#2ea043', headerBg: '#0b1e0b', color: '#b0f0b0', badgeBg: '#3fb95020', badgeColor: '#3fb950', statColor: '#9bc49b', contentBg: '#0a1a0a' },
  failed:    { bg: '#1e0a0a', border: '#f85149', headerBg: '#1a0808', color: '#ffd2d2', badgeBg: '#f8514920', badgeColor: '#f85149', statColor: '#d9908e', contentBg: '#180606' },
  default:   { bg: '#161b22', border: '#30363d', headerBg: '#0d1117', color: '#c9d1d9', badgeBg: '#30363d',   badgeColor: '#6e7681', statColor: '#6e7681', contentBg: '#0d1117' },
}

const STATUS_THEME_LIGHT = {
  running:   { bg: '#e8f4ff', border: '#0969da', headerBg: '#dbeeff', color: '#0550ae', badgeBg: '#d8eaf8', badgeColor: '#0969da', statColor: '#0969da', stripeBg: '#3b8fe8', runShadow: '0 0 8px rgba(9,105,218,0.25)', contentBg: '#f0f8ff' },
  completed: { bg: '#f0fff4', border: '#2da44e', headerBg: '#e6ffed', color: '#1a7f37', badgeBg: '#d4f0db', badgeColor: '#2da44e', statColor: '#1a7f37', contentBg: '#f0fff4' },
  failed:    { bg: '#fff0f0', border: '#cf222e', headerBg: '#ffebe9', color: '#82071e', badgeBg: '#fac0c0', badgeColor: '#cf222e', statColor: '#cf222e', contentBg: '#fff0f0' },
  default:   { bg: '#f6f8fa', border: '#d0d7de', headerBg: '#eaeef2', color: '#24292f', badgeBg: '#d0d7de', badgeColor: '#57606a', statColor: '#57606a', contentBg: '#f6f8fa' },
}

const STATUS_LABEL = {
  running: 'running',
  completed: 'done',
  failed: 'failed',
}

const STATUS_ICON = {
  running:   <Activity size={14} />,
  completed: <CircleCheck size={14} />,
  failed:    <CircleX size={14} />,
}

export default function PromptOutputNode({ data }) {
  if (!data) return null
  const { outputText, errorText, status, durationStr, agentName, timestamp } = data
  const { theme: colorMode } = useTheme()

  const STATUS_THEME = colorMode === 'light' ? STATUS_THEME_LIGHT : STATUS_THEME_DARK
  const t = STATUS_THEME[status] ?? STATUS_THEME.default
  const icon = STATUS_ICON[status] ?? STATUS_ICON.running

  const timestampStr = timestamp
    ? new Date(timestamp).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : null

  const displayText = outputText ?? (status === 'failed' ? errorText : null)

  return (
    <div
      style={{
        width: 800,
        borderRadius: 8,
        border: `2px solid ${t.border}`,
        background: t.bg,
        color: t.color,
        overflow: 'hidden',
        fontSize: 12,
        boxShadow: status === 'running' ? (t.runShadow ?? '0 0 10px rgba(88,166,255,0.4)') : '0 2px 6px rgba(0,0,0,0.15)',
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />

      {/* Running stripe */}
      {status === 'running' && (
        <div style={{
          height: 3,
          background: t.stripeBg ?? t.border,
          backgroundImage:
            'linear-gradient(45deg, rgba(255,255,255,.25) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.25) 50%, rgba(255,255,255,.25) 75%, transparent 75%, transparent)',
          backgroundSize: '1rem 1rem',
          animation: 'stripes 1s linear infinite',
        }} />
      )}

      {/* Header */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 7,
        padding: '0 10px',
        height: 36,
        background: t.headerBg,
        borderBottom: `1px solid ${t.border}33`,
        flexShrink: 0,
      }}>
        <span style={{ flexShrink: 0, display: 'flex', opacity: 0.9 }}>{icon}</span>
        <span style={{ fontWeight: 600, fontSize: 12, flex: 1 }}>Output</span>
        {agentName && (
          <span style={{
            fontSize: 10,
            padding: '2px 6px',
            borderRadius: 10,
            background: `${t.badgeBg}`,
            color: t.statColor,
            flexShrink: 0,
            fontFamily: 'monospace',
          }}>
            {agentName.replace(/_/g, ' ')}
          </span>
        )}
        <span style={{
          fontSize: 9,
          fontWeight: 600,
          padding: '2px 6px',
          borderRadius: 10,
          background: t.badgeBg,
          color: t.badgeColor,
          flexShrink: 0,
          whiteSpace: 'nowrap',
        }}>
          {STATUS_LABEL[status] ?? status}
        </span>
        {durationStr && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 3, fontSize: 10, color: t.statColor, flexShrink: 0 }}>
            <Timer size={11} />
            {durationStr}
          </span>
        )}
        {timestampStr && (
          <span style={{ fontSize: 10, color: t.statColor, flexShrink: 0 }}>{timestampStr}</span>
        )}
      </div>

      {/* Content */}
      <div
        style={{
          padding: '10px 14px',
          background: t.contentBg,
          lineHeight: 1.5,
        }}
        className={[
          'prose prose-sm max-w-none',
          colorMode === 'light' ? '' : 'prose-invert',
          '[&_h1]:text-sm [&_h1]:font-semibold [&_h1]:mt-3 [&_h1]:mb-1',
          '[&_h2]:text-xs [&_h2]:font-semibold [&_h2]:mt-2 [&_h2]:mb-1',
          '[&_h3]:text-xs [&_h3]:font-semibold [&_h3]:mt-1.5 [&_h3]:mb-0.5',
          '[&_p]:text-xs [&_p]:my-1',
          '[&_li]:text-xs [&_li]:my-0.5',
          '[&_ul]:list-disc [&_ul]:pl-4 [&_ol]:list-decimal [&_ol]:pl-4',
          '[&_code]:text-xs [&_code]:font-mono [&_code]:px-1 [&_code]:rounded [&_:not(pre)>code]:break-words [&_:not(pre)>code]:whitespace-pre-wrap',
          colorMode === 'light'
            ? '[&_code]:bg-gray-100 [&_pre]:bg-gray-100'
            : '[&_code]:bg-gray-800 [&_pre]:bg-gray-800',
          '[&_pre]:p-2 [&_pre]:rounded [&_pre]:overflow-x-auto [&_pre]:my-1',
          '[&_blockquote]:border-l-2 [&_blockquote]:pl-3 [&_blockquote]:my-1',
          '[&_strong]:font-semibold',
          '[&_table]:text-xs [&_th]:font-medium [&_th]:pb-1 [&_td]:py-0.5',
        ].join(' ')}
      >
        {displayText ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{displayText}</ReactMarkdown>
        ) : status === 'running' ? (
          <span style={{ color: t.statColor, fontStyle: 'italic', fontSize: 12 }}>Waiting for output…</span>
        ) : (
          <span style={{ color: t.statColor, fontStyle: 'italic', fontSize: 12 }}>No output available</span>
        )}
      </div>

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}
