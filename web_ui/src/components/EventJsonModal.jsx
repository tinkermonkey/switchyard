import Modal from './Modal'

export default function EventJsonModal({ event, onClose }) {
  if (!event) return null

  const json = JSON.stringify(event, null, 2)
  const title = event.event_type
    ? event.event_type.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
    : 'Event JSON'

  return (
    <Modal title={title} onClose={onClose}>
      <pre className="text-xs font-mono bg-gh-canvas-subtle p-4 rounded border border-gh-border overflow-x-auto whitespace-pre-wrap">
        {json}
      </pre>
      <div className="flex justify-end gap-2 mt-4">
        <button
          onClick={() => navigator.clipboard.writeText(json)}
          className="px-4 py-2 bg-gh-accent-emphasis text-white rounded hover:bg-opacity-90 transition-colors"
        >
          Copy to Clipboard
        </button>
        <button
          onClick={onClose}
          className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded hover:bg-gh-border-muted transition-colors"
        >
          Close
        </button>
      </div>
    </Modal>
  )
}
