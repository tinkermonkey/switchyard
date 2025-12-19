import { useEffect } from 'react'
import { CheckCircle, AlertCircle, Info, X } from 'lucide-react'

export default function Toast({
  show,
  onClose,
  message,
  type = 'info', // 'success', 'error', 'info'
  duration = 5000
}) {
  useEffect(() => {
    if (show && duration > 0) {
      const timer = setTimeout(onClose, duration)
      return () => clearTimeout(timer)
    }
  }, [show, duration, onClose])

  if (!show) return null

  const icons = {
    success: <CheckCircle className="w-5 h-5 text-green-500" />,
    error: <AlertCircle className="w-5 h-5 text-red-500" />,
    info: <Info className="w-5 h-5 text-blue-500" />
  }

  const bgColors = {
    success: 'bg-green-500/10 border-green-500/20',
    error: 'bg-red-500/10 border-red-500/20',
    info: 'bg-blue-500/10 border-blue-500/20'
  }

  return (
    <div className="fixed top-4 right-4 z-50 animate-in slide-in-from-top duration-300">
      <div className={`flex items-center gap-3 px-4 py-3 border rounded-lg shadow-lg min-w-[300px] max-w-md ${bgColors[type]}`}>
        {icons[type]}
        <p className="flex-1 text-sm text-gh-fg">{message}</p>
        <button
          onClick={onClose}
          className="text-gh-fg-muted hover:text-gh-fg transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}
