export default function PipelinesStatus({ pipelines }) {
  return (
    <div className="bg-gh-canvas border border-gh-border rounded-md p-3">
      <span className="text-xs font-medium text-gh-fg-muted block mb-2">Pipelines</span>
      {pipelines && pipelines.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {pipelines.map((pipeline) => (
            <span
              key={pipeline}
              className="inline-block px-2 py-1 rounded-md text-xs bg-gh-accent-emphasis/10 text-gh-accent-fg border border-gh-accent-emphasis/20"
            >
              {pipeline}
            </span>
          ))}
        </div>
      ) : (
        <p className="text-xs text-gh-fg-muted">No pipelines configured</p>
      )}
    </div>
  )
}
