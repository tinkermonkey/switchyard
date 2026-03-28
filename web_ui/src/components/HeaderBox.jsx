/**
 * HeaderBox - Base component for header stat/info boxes
 */
export default function HeaderBox({ title, children, className = '', minWidth = 'md:min-w-[140px]' }) {
  return (
    <div className={`bg-gh-canvas p-2 md:p-3 rounded-md border border-gh-border min-w-0 overflow-hidden ${minWidth} ${className}`}>
      {title && (
        <h3 className="text-gh-fg-muted text-xs uppercase mb-1">
          {title}
        </h3>
      )}
      {children}
    </div>
  )
}
