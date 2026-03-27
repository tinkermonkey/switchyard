import { Handle, Position } from '@xyflow/react'
import { FileText } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useTheme } from '../../../contexts'

function formatChars(n) {
  if (n == null) return null
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

const THEME_DARK = {
  bg: '#161b22',
  border: '#30363d',
  headerBg: '#0d1117',
  color: '#c9d1d9',
  pillBg: '#21262d',
  pillColor: '#8b949e',
  statColor: '#8b949e',
  mdFileBg: '#1c2128',
  mdFileColor: '#79c0ff',
  mdFileBorder: '#264468',
  contentBg: '#0d1117',
}

const THEME_LIGHT = {
  bg: '#f6f8fa',
  border: '#d0d7de',
  headerBg: '#eaeef2',
  color: '#24292f',
  pillBg: '#e0e6eb',
  pillColor: '#57606a',
  statColor: '#57606a',
  mdFileBg: '#dff0ff',
  mdFileColor: '#0550ae',
  mdFileBorder: '#a5c8f8',
  contentBg: '#f6f8fa',
}

export default function PromptInputNode({ data }) {
  if (!data) return null
  const { promptText, promptComponents, mdFiles, agentName, timestamp } = data
  const { theme: colorMode } = useTheme()
  const t = colorMode === 'light' ? THEME_LIGHT : THEME_DARK

  const timestampStr = timestamp
    ? new Date(timestamp).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : null

  const sysChars  = formatChars(promptComponents?.system_prompt_chars)
  const ctxChars  = formatChars(promptComponents?.context_chars)
  const taskChars = formatChars(promptComponents?.task_chars)
  const hasComponents = sysChars || ctxChars || taskChars

  const hasMdFiles = mdFiles?.length > 0

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
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />

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
        <span style={{ flexShrink: 0, display: 'flex', opacity: 0.7 }}>
          <FileText size={14} />
        </span>
        <span style={{ fontWeight: 600, fontSize: 12, flex: 1 }}>Input Prompt</span>
        {agentName && (
          <span style={{
            fontSize: 10,
            padding: '2px 6px',
            borderRadius: 10,
            background: t.pillBg,
            color: t.pillColor,
            flexShrink: 0,
            fontFamily: 'monospace',
          }}>
            {agentName.replace(/_/g, ' ')}
          </span>
        )}
        {timestampStr && (
          <span style={{ fontSize: 10, color: t.statColor, flexShrink: 0 }}>{timestampStr}</span>
        )}
      </div>

      {/* Markdown file chips */}
      {hasMdFiles && (
        <div style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 5,
          padding: '6px 10px',
          borderBottom: `1px solid ${t.border}33`,
          background: t.headerBg,
        }}>
          <span style={{ fontSize: 10, color: t.statColor, alignSelf: 'center', marginRight: 2 }}>
            Context files:
          </span>
          {mdFiles.map(file => (
            <span
              key={file}
              style={{
                fontSize: 10,
                fontFamily: 'monospace',
                padding: '2px 6px',
                borderRadius: 4,
                background: t.mdFileBg,
                color: t.mdFileColor,
                border: `1px solid ${t.mdFileBorder}`,
              }}
            >
              {file}
            </span>
          ))}
        </div>
      )}

      {/* Prompt component breakdown */}
      {hasComponents && (
        <div style={{
          display: 'flex',
          gap: 16,
          padding: '5px 10px',
          borderBottom: `1px solid ${t.border}22`,
          background: t.headerBg,
        }}>
          {sysChars && (
            <span style={{ fontSize: 10, color: t.statColor }}>
              System: <span style={{ color: t.color }}>{sysChars} chars</span>
            </span>
          )}
          {ctxChars && (
            <span style={{ fontSize: 10, color: t.statColor }}>
              Context: <span style={{ color: t.color }}>{ctxChars} chars</span>
            </span>
          )}
          {taskChars && (
            <span style={{ fontSize: 10, color: t.statColor }}>
              Task: <span style={{ color: t.color }}>{taskChars} chars</span>
            </span>
          )}
        </div>
      )}

      {/* Prompt content */}
      <div
        style={{
          maxHeight: 600,
          overflowY: 'auto',
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
          '[&_ul]:pl-4 [&_ol]:pl-4',
          '[&_code]:text-xs [&_code]:font-mono [&_code]:px-1 [&_code]:rounded',
          colorMode === 'light'
            ? '[&_code]:bg-gray-100 [&_pre]:bg-gray-100'
            : '[&_code]:bg-gray-800 [&_pre]:bg-gray-800',
          '[&_pre]:p-2 [&_pre]:rounded [&_pre]:overflow-x-auto [&_pre]:my-1',
          '[&_blockquote]:border-l-2 [&_blockquote]:pl-3 [&_blockquote]:my-1',
          '[&_strong]:font-semibold',
          '[&_table]:text-xs [&_th]:font-medium [&_th]:pb-1 [&_td]:py-0.5',
        ].join(' ')}
      >
        {promptText ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{promptText}</ReactMarkdown>
        ) : (
          <span style={{ color: t.statColor, fontStyle: 'italic' }}>No prompt text available</span>
        )}
      </div>

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}
