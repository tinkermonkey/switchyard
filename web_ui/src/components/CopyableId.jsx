import { useState } from 'react'
import { Copy, Check } from 'lucide-react'

function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text)
  }
  // Fallback for non-secure contexts
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  document.execCommand('copy')
  document.body.removeChild(textarea)
  return Promise.resolve()
}

export default function CopyableId({ id, className = '' }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = (e) => {
    e.stopPropagation()
    copyToClipboard(id).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <span className={`inline-flex items-center gap-1 font-mono ${className}`}>
      <span>{id}</span>
      <button
        onClick={handleCopy}
        className="text-gh-fg-muted hover:text-gh-fg transition-colors flex-shrink-0"
        title={copied ? 'Copied!' : 'Copy'}
      >
        {copied ? <Check className="w-3 h-3 text-green-500" /> : <Copy className="w-3 h-3" />}
      </button>
    </span>
  )
}
