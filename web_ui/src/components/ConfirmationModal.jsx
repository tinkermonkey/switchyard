import { useEffect } from 'react'
import { AlertTriangle, X } from 'lucide-react'

export default function ConfirmationModal({ 
  show, 
  onClose, 
  onConfirm, 
  title, 
  message, 
  confirmText = "Confirm", 
  cancelText = "Cancel",
  isDangerous = false 
}) {
  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape') onClose()
    }
    if (show) {
      document.addEventListener('keydown', handleEscape)
    }
    return () => document.removeEventListener('keydown', handleEscape)
  }, [show, onClose])

  if (!show) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
      <div className="relative w-full max-w-md bg-gh-canvas border border-gh-border rounded-lg shadow-lg animate-in fade-in zoom-in duration-200">
        <button 
          onClick={onClose}
          className="absolute top-3 right-3 text-gh-fg-muted hover:text-gh-fg transition-colors"
        >
          <X className="w-5 h-5" />
        </button>
        
        <div className="p-6 text-center">
          <AlertTriangle className={`mx-auto mb-4 h-14 w-14 ${isDangerous ? 'text-red-500' : 'text-gh-fg-muted'}`} />
          
          <h3 className="mb-2 text-lg font-semibold text-gh-fg">
            {title || "Confirm Action"}
          </h3>
          
          <p className="mb-6 text-sm text-gh-fg-muted">
            {message}
          </p>
          
          <div className="flex justify-center gap-3">
            <button
              onClick={() => {
                onConfirm()
                onClose()
              }}
              className={`px-4 py-2 text-sm font-medium text-white rounded-md transition-colors ${
                isDangerous 
                  ? 'bg-red-600 hover:bg-red-700' 
                  : 'bg-gh-accent-emphasis hover:bg-gh-accent-primary'
              }`}
            >
              {confirmText}
            </button>
            {cancelText && (
              <button
                onClick={onClose}
                className="px-4 py-2 text-sm font-medium text-gh-fg bg-gh-canvas-subtle border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors"
              >
                {cancelText}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
