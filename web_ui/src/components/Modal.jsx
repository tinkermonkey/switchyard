import { useEffect } from 'react'
import { X } from 'lucide-react'

export default function Modal({ title, children, onClose }) {
  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-80 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-gh-canvas-subtle border border-gh-border rounded-md w-full max-w-6xl h-[95vh] md:h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center p-3 md:p-5 border-b border-gh-border flex-shrink-0">
          <h3 className="text-gh-accent-primary text-lg font-semibold">{title}</h3>
          <button
            onClick={onClose}
            className="text-gh-fg-muted hover:text-gh-fg hover:bg-gh-border-muted rounded p-1 transition-colors"
          >
            <X className="w-6 h-6" />
          </button>
        </div>
        <div className="overflow-auto p-3 md:p-5 flex-1">{children}</div>
      </div>
    </div>
  )
}
