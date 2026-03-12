import { useState } from 'react'
import { createPortal } from 'react-dom'
import { Handle, Position } from '@xyflow/react'
import { PlusCircle } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import EventJsonModal from '../../EventJsonModal'

const NODE_STYLE = {
  padding: '12px 16px',
  borderRadius: '8px',
  border: '2px solid',
  minWidth: '200px',
  maxWidth: '300px',
  background: '#06b6d4',
  borderColor: '#0891b2',
  color: '#fff',
}

export default function SubIssueCreatedNode({ data }) {
  const [hovered, setHovered] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)

  if (!data) return null

  const { label, metadata, isActive, event } = data

  const issueTitle = event?.inputs?.title || label
  const issueUrl = (() => {
    if (event?.issue_url) return event.issue_url
    const prUrl = event?.inputs?.pr_url
    if (prUrl && event?.issue_number) {
      const base = prUrl.replace(/\/pull\/\d+$/, '')
      return `${base}/issues/${event.issue_number}`
    }
    return null
  })()
  const issueBody = event?.inputs?.body || ''

  const nodeStyle = {
    ...NODE_STYLE,
    boxShadow: isActive ? '0 0 10px rgba(88, 166, 255, 0.5)' : '0 2px 4px rgba(0,0,0,0.1)',
    ...(event && { cursor: 'pointer' }),
  }

  return (
    <>
      <div
        className="relative"
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      >
        <div
          style={nodeStyle}
          className="relative"
          onClick={event ? (e) => { e.stopPropagation(); setModalOpen(true) } : undefined}
        >
          {isActive && (
            <div
              className="absolute top-0 left-0 right-0 h-1 rounded-t-md overflow-hidden"
              style={{
                backgroundImage:
                  'linear-gradient(45deg, rgba(255,255,255,.2) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.2) 50%, rgba(255,255,255,.2) 75%, transparent 75%, transparent)',
                backgroundSize: '1rem 1rem',
                animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite, stripes 1s linear infinite',
              }}
            />
          )}

          <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />

          <div className="flex items-start gap-2">
            <div className="mt-0.5 shrink-0">
              <PlusCircle className="w-4 h-4" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-semibold text-sm">
                {issueUrl ? (
                  <a
                    href={issueUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    {issueTitle}
                  </a>
                ) : (
                  issueTitle
                )}
              </div>
              {metadata && <div className="text-xs mt-1 opacity-90">{metadata}</div>}
            </div>
          </div>

          <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
        </div>

        {hovered && issueBody && (
          <div
            className="absolute top-0 z-50 bg-gh-canvas-default border border-gh-border rounded-lg shadow-xl p-4 text-gh-fg-default"
            style={{ left: 'calc(100% + 8px)', width: '320px', maxHeight: '400px', overflow: 'auto' }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="text-xs font-semibold text-gh-fg-muted mb-2 uppercase tracking-wide">Issue Body</div>
            <div className="text-sm prose prose-sm max-w-none prose-headings:text-gh-fg-default prose-p:text-gh-fg-default prose-code:text-gh-accent-fg prose-pre:bg-gh-canvas-subtle">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{issueBody}</ReactMarkdown>
            </div>
          </div>
        )}
      </div>

      {modalOpen && createPortal(
        <EventJsonModal event={event} onClose={() => setModalOpen(false)} />,
        document.body
      )}
    </>
  )
}
