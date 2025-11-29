import { useEffect, useState } from 'react'
import { FileText, AlertCircle, CheckCircle, XCircle } from 'lucide-react'
import ReactMarkdown from 'react-markdown'

export default function ClaudeRecommendations({ fingerprintId }) {
  const [recommendations, setRecommendations] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchRecommendations()
  }, [fingerprintId])

  const fetchRecommendations = async () => {
    try {
      setLoading(true)
      const response = await fetch(`/api/medic/claude/investigations/${fingerprintId}/recommendations`)
      if (!response.ok) {
        if (response.status === 404) {
          setRecommendations(null)
          setError('No recommendations available yet')
        } else {
          throw new Error('Failed to fetch recommendations')
        }
      } else {
        const data = await response.json()
        setRecommendations(data)
        setError(null)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 bg-yellow-500/10 border border-yellow-500/20 rounded-lg">
        <div className="flex items-center gap-2">
          <AlertCircle className="w-4 h-4 text-yellow-500" />
          <p className="text-sm text-yellow-500">{error}</p>
        </div>
      </div>
    )
  }

  if (!recommendations) {
    return (
      <div className="p-8 text-center bg-gh-canvas-subtle border border-gh-border rounded-lg">
        <FileText className="w-8 h-8 text-gh-fg-muted mx-auto mb-2" />
        <p className="text-sm text-gh-fg-muted">No recommendations available</p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-lg font-semibold text-gh-fg flex items-center gap-2">
              <FileText className="w-5 h-5" />
              Recommendations
            </h3>
            {recommendations.created_at && (
              <p className="text-xs text-gh-fg-muted mt-1">
                Generated: {new Date(recommendations.created_at).toLocaleString()}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Markdown Content */}
      <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-6">
        <div className="prose prose-sm max-w-none prose-invert">
          <ReactMarkdown
            components={{
              // Custom rendering for code blocks
              code: ({node, inline, className, children, ...props}) => {
                if (inline) {
                  return (
                    <code className="px-1.5 py-0.5 bg-gh-canvas border border-gh-border rounded text-sm font-mono" {...props}>
                      {children}
                    </code>
                  )
                }
                return (
                  <pre className="bg-gh-canvas border border-gh-border rounded p-4 overflow-x-auto">
                    <code className="text-sm font-mono" {...props}>
                      {children}
                    </code>
                  </pre>
                )
              },
              // Custom rendering for headings
              h1: ({children}) => <h1 className="text-2xl font-bold text-gh-fg mb-4 mt-6">{children}</h1>,
              h2: ({children}) => <h2 className="text-xl font-bold text-gh-fg mb-3 mt-5">{children}</h2>,
              h3: ({children}) => <h3 className="text-lg font-semibold text-gh-fg mb-2 mt-4">{children}</h3>,
              h4: ({children}) => <h4 className="text-base font-semibold text-gh-fg mb-2 mt-3">{children}</h4>,
              // Custom rendering for paragraphs
              p: ({children}) => <p className="text-sm text-gh-fg mb-3">{children}</p>,
              // Custom rendering for lists
              ul: ({children}) => <ul className="list-disc list-inside text-sm text-gh-fg mb-3 space-y-1">{children}</ul>,
              ol: ({children}) => <ol className="list-decimal list-inside text-sm text-gh-fg mb-3 space-y-1">{children}</ol>,
              li: ({children}) => <li className="text-gh-fg">{children}</li>,
              // Custom rendering for links
              a: ({href, children}) => (
                <a href={href} className="text-gh-accent-fg hover:underline" target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ),
              // Custom rendering for blockquotes
              blockquote: ({children}) => (
                <blockquote className="border-l-4 border-gh-accent-emphasis pl-4 my-3 text-gh-fg-muted italic">
                  {children}
                </blockquote>
              ),
            }}
          >
            {recommendations.content}
          </ReactMarkdown>
        </div>
      </div>

      {/* Actions */}
      <div className="flex gap-3">
        <button
          className="px-4 py-2 bg-green-500/10 text-green-500 border border-green-500/20 rounded hover:bg-green-500/20 transition-colors text-sm flex items-center gap-2"
          onClick={() => alert('Mark as resolved functionality to be implemented')}
        >
          <CheckCircle className="w-4 h-4" />
          Mark as Resolved
        </button>
        <button
          className="px-4 py-2 bg-gray-500/10 text-gray-500 border border-gray-500/20 rounded hover:bg-gray-500/20 transition-colors text-sm flex items-center gap-2"
          onClick={() => alert('Ignore functionality to be implemented')}
        >
          <XCircle className="w-4 h-4" />
          Ignore
        </button>
      </div>
    </div>
  )
}
