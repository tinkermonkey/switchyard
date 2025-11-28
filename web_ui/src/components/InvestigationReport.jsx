import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'

export default function InvestigationReport({ fingerprintId, reportType }) {
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchReport()
  }, [fingerprintId, reportType])

  const fetchReport = async () => {
    try {
      setLoading(true)
      const response = await fetch(`/api/medic/investigations/${fingerprintId}/${reportType}`)
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error('Report not found')
        }
        throw new Error('Failed to fetch report')
      }
      const data = await response.json()
      setContent(data.content || '')
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8 bg-gh-canvas-subtle border border-gh-border rounded-lg">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
        <p className="text-sm text-red-500">Error: {error}</p>
      </div>
    )
  }

  return (
    <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-6">
      <div className="prose prose-invert prose-sm max-w-none">
        <ReactMarkdown
          components={{
            code({ node, inline, className, children, ...props }) {
              const match = /language-(\w+)/.exec(className || '')
              return !inline && match ? (
                <SyntaxHighlighter
                  style={vscDarkPlus}
                  language={match[1]}
                  PreTag="div"
                  {...props}
                >
                  {String(children).replace(/\n$/, '')}
                </SyntaxHighlighter>
              ) : (
                <code className="bg-gh-canvas px-1.5 py-0.5 rounded text-gh-accent-fg font-mono text-xs" {...props}>
                  {children}
                </code>
              )
            },
            h1: ({ children }) => (
              <h1 className="text-2xl font-bold text-gh-fg mt-6 mb-4 pb-2 border-b border-gh-border">
                {children}
              </h1>
            ),
            h2: ({ children }) => (
              <h2 className="text-xl font-bold text-gh-fg mt-5 mb-3">
                {children}
              </h2>
            ),
            h3: ({ children }) => (
              <h3 className="text-lg font-semibold text-gh-fg mt-4 mb-2">
                {children}
              </h3>
            ),
            p: ({ children }) => (
              <p className="text-gh-fg mb-4 leading-relaxed">
                {children}
              </p>
            ),
            ul: ({ children }) => (
              <ul className="list-disc list-inside text-gh-fg mb-4 space-y-1">
                {children}
              </ul>
            ),
            ol: ({ children }) => (
              <ol className="list-decimal list-inside text-gh-fg mb-4 space-y-1">
                {children}
              </ol>
            ),
            li: ({ children }) => (
              <li className="text-gh-fg ml-4">
                {children}
              </li>
            ),
            blockquote: ({ children }) => (
              <blockquote className="border-l-4 border-gh-accent-emphasis pl-4 my-4 text-gh-fg-muted italic">
                {children}
              </blockquote>
            ),
            a: ({ href, children }) => (
              <a
                href={href}
                className="text-gh-accent-fg hover:underline"
                target="_blank"
                rel="noopener noreferrer"
              >
                {children}
              </a>
            ),
            table: ({ children }) => (
              <div className="overflow-x-auto my-4">
                <table className="min-w-full border border-gh-border">
                  {children}
                </table>
              </div>
            ),
            thead: ({ children }) => (
              <thead className="bg-gh-canvas-subtle">
                {children}
              </thead>
            ),
            th: ({ children }) => (
              <th className="border border-gh-border px-4 py-2 text-left text-gh-fg font-semibold">
                {children}
              </th>
            ),
            td: ({ children }) => (
              <td className="border border-gh-border px-4 py-2 text-gh-fg">
                {children}
              </td>
            ),
            hr: () => (
              <hr className="my-6 border-gh-border" />
            ),
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    </div>
  )
}
