import { useState } from 'react'
import { createPortal } from 'react-dom'
import { Handle, Position } from '@xyflow/react'
import { PlusCircle } from 'lucide-react'
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

  const nodeStyle = {
    ...NODE_STYLE,
    boxShadow: isActive ? '0 0 10px rgba(88, 166, 255, 0.5)' : '0 2px 4px rgba(0,0,0,0.1)',
    ...(event && { cursor: 'pointer' }),
  }

  return (
    <>
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

      {modalOpen && createPortal(
        <EventJsonModal event={event} onClose={() => setModalOpen(false)} />,
        document.body
      )}
    </>
  )
}
