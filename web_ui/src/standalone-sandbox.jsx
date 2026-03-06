import { Upload, RotateCcw, Download } from 'lucide-react'
import PipelineFlowGraph, { DEFAULT_LAYOUT_OPTIONS } from './components/PipelineFlowGraph'
import { useState, useEffect, useCallback } from 'react'
import { toggleCycleCollapsed } from './utils/cycleLayout'
import { buildFlowchart } from './utils/buildFlowchart'

// Human-readable labels for the layout parameter sliders.
// Only keys present here will show as sliders; the rest of DEFAULT_LAYOUT_OPTIONS
// are still passed to the layout algorithm as invisible defaults.
const PARAM_LABELS = {
  horizontalSpacing: 'H Spacing (within cycle)',
  innerVertSpacing: 'V Spacing (within iteration)',
  cycleGap: 'V Spacing (between cycles)',
  cyclePadding: 'Cycle Padding',
  viewportWidth: 'Viewport Width',
}

export default function StandaloneSandbox() {
  const [debugData, setDebugData] = useState(null)
  const [layoutParams, setLayoutParams] = useState(DEFAULT_LAYOUT_OPTIONS)
  const [cycles, setCycles] = useState(new Map())
  const [isDragOver, setIsDragOver] = useState(false)
  const [error, setError] = useState(null)
  const [rawBuild, setRawBuild] = useState(null)
  const [nodesDraggable, setNodesDraggable] = useState(false)
  const [processedModel, setProcessedModel] = useState(null)

  const handleToggleCycle = useCallback((cycleId) => {
    setCycles(prev => toggleCycleCollapsed(prev, cycleId))
  }, [])

  const loadFile = useCallback((file) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = (e) => {
      try {
        const json = JSON.parse(e.target.result)
        if (!json.pipelineRun || !Array.isArray(json.events)) {
          setError('Invalid debug export: missing pipelineRun or events fields')
          return
        }
        setDebugData(json)
        setError(null)
      } catch {
        setError('Failed to parse JSON file')
      }
    }
    reader.readAsText(file)
  }, [])

  const handleDrop = useCallback((e) => {
    e.preventDefault()
    setIsDragOver(false)
    loadFile(e.dataTransfer.files[0])
  }, [loadFile])

  const handleFileInput = useCallback((e) => {
    loadFile(e.target.files[0])
    e.target.value = ''
  }, [loadFile])

  // Reset cycles when new data is loaded
  useEffect(() => {
    setCycles(new Map())
    setRawBuild(null)
    setProcessedModel(null)
  }, [debugData])

  // Build raw (unpositioned) flowchart whenever data or cycles change
  useEffect(() => {
    if (!debugData) { setRawBuild(null); return }
    const { pipelineRun, events, workflowConfig } = debugData
    if (!pipelineRun || !events) return

    // Exclude claude_log streaming events — irrelevant to graph building, but can
    // account for 80%+ of events in large runs and cause layout thread exhaustion.
    const graphEvents = events.filter(e => e.event_category !== 'claude_log')

    const result = buildFlowchart({
      events: graphEvents,
      existingCycles: cycles,
      workflowConfig: workflowConfig || null,
      selectedPipelineRun: pipelineRun,
      activeAgentNames: new Set(),
    })

    const { updatedCycles } = result
    const hasNewCycles = updatedCycles.size > 0 &&
      [...updatedCycles.keys()].some(k => !cycles.has(k))
    if (hasNewCycles) {
      setCycles(updatedCycles)
      return
    }

    setRawBuild(result)
    setProcessedModel(result.model ?? null)
  }, [debugData, cycles])

  const handleDownloadProcessed = useCallback(() => {
    if (!processedModel || !debugData) return
    const { prelude, cycles, postlude, agentExecutions } = processedModel
    const serializable = {
      pipelineRun: debugData.pipelineRun,
      prelude,
      cycles: [...(cycles?.values?.() ?? cycles ?? [])],
      postlude,
      agentExecutions: agentExecutions instanceof Map
        ? Object.fromEntries(agentExecutions)
        : agentExecutions,
    }
    const blob = new Blob([JSON.stringify(serializable, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const runId = debugData.pipelineRun?.pipeline_run_id ?? 'pipeline'
    a.download = `${runId}-processed.json`
    a.click()
    URL.revokeObjectURL(url)
  }, [processedModel, debugData])

  const handleParamChange = useCallback((key, value) => {
    const parsed = parseInt(value, 10)
    setLayoutParams(prev => ({ ...prev, [key]: isNaN(parsed) ? 0 : parsed }))
  }, [])

  return (
    <div className="flex flex-col bg-gh-canvas text-gh-fg" style={{ height: '100vh' }}>
      <div className="flex gap-4 flex-1 p-4 min-h-0">
        {/* Controls Panel */}
        <div className="w-60 flex-shrink-0 bg-gh-canvas-subtle rounded-md border border-gh-border p-4 flex flex-col gap-4 overflow-y-auto">
          <h1 className="text-base font-bold">Pipeline Layout Sandbox</h1>

          {/* File Drop Zone */}
          <div
            onDragOver={(e) => { e.preventDefault(); setIsDragOver(true) }}
            onDragLeave={() => setIsDragOver(false)}
            onDrop={handleDrop}
            className={`border-2 border-dashed rounded-md p-4 text-center transition-colors ${
              isDragOver
                ? 'border-gh-accent-primary bg-blue-900/10'
                : 'border-gh-border hover:border-gh-accent-primary cursor-pointer'
            }`}
          >
            <label className="cursor-pointer block">
              <Upload className="w-6 h-6 mx-auto mb-2 opacity-50" />
              <div className="text-sm">Drop debug JSON here</div>
              <div className="text-xs text-gh-fg-muted mt-1">or click to browse</div>
              <input
                type="file"
                accept=".json,application/json"
                className="hidden"
                onChange={handleFileInput}
              />
            </label>
          </div>

          {error && (
            <div className="text-xs text-red-400 bg-red-900/10 border border-red-800 rounded px-2 py-1">
              {error}
            </div>
          )}

          {debugData && (
            <div className="text-xs text-gh-fg-muted bg-gh-canvas border border-gh-border rounded px-2 py-1">
              <div className="font-medium text-gh-fg truncate">
                {debugData.pipelineRun?.issue_title || 'Unknown pipeline'}
              </div>
              <div className="mt-0.5">
                {debugData.events?.length ?? 0} events
                {debugData.pipelineRun?.project && ` • ${debugData.pipelineRun.project}`}
              </div>
            </div>
          )}

          {/* Interaction Controls */}
          <div>
            <h3 className="text-sm font-semibold mb-3">Interaction</h3>
            <label className="flex items-center gap-2 text-sm cursor-pointer select-none">
              <input
                type="checkbox"
                checked={nodesDraggable}
                onChange={(e) => setNodesDraggable(e.target.checked)}
                className="w-4 h-4 accent-gh-accent-primary"
              />
              Draggable &amp; resizable nodes
            </label>
          </div>

          {/* Layout Parameters */}
          <div>
            <h3 className="text-sm font-semibold mb-3">Layout Parameters</h3>
            <div className="space-y-3">
              {Object.keys(PARAM_LABELS).map(key => [key, layoutParams[key]]).map(([key, value]) => (
                <div key={key}>
                  <label className="text-xs text-gh-fg-muted block mb-1">
                    {PARAM_LABELS[key] || key}
                  </label>
                  <input
                    type="number"
                    value={value}
                    min={0}
                    onChange={(e) => handleParamChange(key, e.target.value)}
                    className="w-full px-2 py-1 text-sm bg-gh-canvas border border-gh-border rounded focus:outline-none focus:border-gh-accent-primary"
                  />
                </div>
              ))}
            </div>
          </div>

          {/* Reset Button */}
          <button
            onClick={() => setLayoutParams(DEFAULT_LAYOUT_OPTIONS)}
            className="px-4 py-2 bg-gh-canvas border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm flex items-center justify-center gap-2"
          >
            <RotateCcw className="w-4 h-4" />
            Reset to Defaults
          </button>

          {/* Download Processed Data */}
          <button
            onClick={handleDownloadProcessed}
            disabled={!processedModel}
            className="px-4 py-2 bg-gh-canvas border border-gh-border rounded-md hover:bg-gh-border-muted transition-colors text-sm flex items-center justify-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Download className="w-4 h-4" />
            Download Processed Data
          </button>
        </div>

        {/* Graph Preview */}
        <div className="flex-1 bg-gh-canvas-subtle rounded-md border border-gh-border overflow-hidden">
          {!debugData ? (
            <div className="flex flex-col items-center justify-center h-full text-gh-fg-muted">
              <Upload className="w-12 h-12 mb-3 opacity-30" />
              <p className="text-base">Load a debug export to preview the graph</p>
              <p className="text-sm mt-1 opacity-75">
                Use the "📥 Download Debug Data" button on Pipeline Run Graphs
              </p>
            </div>
          ) : (
            <PipelineFlowGraph
              rawBuild={rawBuild}
              onToggleCycle={handleToggleCycle}
              layoutOptions={layoutParams}
              nodesDraggable={nodesDraggable}
              allowResizing={nodesDraggable}
              height="100%"
              emptyMessage="No renderable events found in the debug data"
            />
          )}
        </div>
      </div>
    </div>
  )
}
