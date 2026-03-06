import React, { useMemo } from 'react'
import { CheckCircle2, XCircle, Loader2, AlertTriangle, Clock, Wrench, PlayCircle } from 'lucide-react'

/**
 * RepairCycleStatus - Visual component to communicate the status of a repair cycle
 * 
 * Parses raw events to reconstruct the state of the repair cycle:
 * - Testing stages (unit, integration, e2e)
 * - Test results (failures, warnings)
 * - Timing information
 * - Fix cycle progress
 */
export default function RepairCycleStatus({ events }) {
  const state = useMemo(() => {
    if (!events || events.length === 0) return null

    const cycleState = {
      isActive: false,
      stages: {}, // Map of test_type -> stage object
      stageOrder: [], // List of test_types in order
      currentStage: null,
      startTime: null,
      endTime: null,
      overallSuccess: null,
      error: null
    }

    // Sort events chronologically
    const sortedEvents = [...events].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

    for (const eventWrapper of sortedEvents) {
      const event = eventWrapper.raw_event?.event || eventWrapper
      const eventType = event.event_type || event.type
      // Fallback to event object if data/payload is missing (some events are flat)
      const data = event.data || event.payload || event
      const timestamp = event.timestamp || eventWrapper.timestamp

      // Check if this is a repair cycle event
      if (!eventType || !eventType.startsWith('repair_cycle')) continue

      cycleState.isActive = true
      if (!cycleState.startTime) cycleState.startTime = timestamp

      // Helper to get current iteration object
      const getCurrentIteration = (stage) => {
        if (!stage.history.length) return null;
        return stage.history[stage.history.length - 1];
      };

      // Handle specific event types
      switch (eventType) {
        case 'repair_cycle_test_cycle_started': {
          const testType = data.test_type
          if (!cycleState.stages[testType]) {
            cycleState.stages[testType] = {
              type: testType,
              status: 'running',
              maxIterations: data.max_iterations,
              startTime: timestamp,
              history: [], // Array of iteration objects
            }
            cycleState.stageOrder.push(testType)
          }
          cycleState.currentStage = testType
          break
        }

        case 'repair_cycle_iteration': {
          if (cycleState.currentStage && cycleState.stages[cycleState.currentStage]) {
            const stage = cycleState.stages[cycleState.currentStage]
            const iterNum = data.test_cycle_iteration
            
            // Check if we already have this iteration
            let iteration = stage.history.find(i => i.number === iterNum)
            if (!iteration) {
              iteration = {
                number: iterNum,
                results: null,
                fixCycle: {
                  status: 'idle',
                  filesToFix: 0,
                  filesFixed: 0,
                  warningsReviewed: 0
                }
              }
              stage.history.push(iteration)
            }
          }
          break
        }

        case 'repair_cycle_test_execution_completed': {
          if (cycleState.currentStage && cycleState.stages[cycleState.currentStage]) {
            const stage = cycleState.stages[cycleState.currentStage]
            const iteration = getCurrentIteration(stage)
            if (iteration) {
              iteration.results = {
                passed: data.passed,
                failed: data.failed,
                warnings: data.warnings,
                hasFailures: data.has_failures,
                failures: data.failures || []
              }
            }
          }
          break
        }

        case 'repair_cycle_fix_cycle_started': {
          if (cycleState.currentStage && cycleState.stages[cycleState.currentStage]) {
            const stage = cycleState.stages[cycleState.currentStage]
            const iteration = getCurrentIteration(stage)
            if (iteration) {
              iteration.fixCycle.status = 'fixing'
              iteration.fixCycle.filesToFix = data.file_count
              iteration.fixCycle.filesFixed = 0
            }
          }
          break
        }

        case 'repair_cycle_file_fix_completed': {
          if (cycleState.currentStage && cycleState.stages[cycleState.currentStage]) {
            const stage = cycleState.stages[cycleState.currentStage]
            const iteration = getCurrentIteration(stage)
            if (iteration) {
              iteration.fixCycle.filesFixed = (iteration.fixCycle.filesFixed || 0) + 1
            }
          }
          break
        }
          
        case 'repair_cycle_fix_cycle_completed': {
          if (cycleState.currentStage && cycleState.stages[cycleState.currentStage]) {
            const stage = cycleState.stages[cycleState.currentStage]
            const iteration = getCurrentIteration(stage)
            if (iteration) {
              iteration.fixCycle.status = 'idle'
              iteration.fixCycle.filesFixed = data.files_fixed
            }
          }
          break
        }

        case 'repair_cycle_warning_review_started': {
          if (cycleState.currentStage && cycleState.stages[cycleState.currentStage]) {
            const stage = cycleState.stages[cycleState.currentStage]
            const iteration = getCurrentIteration(stage)
            if (iteration) {
              iteration.fixCycle.status = 'reviewing_warnings'
              iteration.fixCycle.warnings = data.warnings || []
            }
          }
          break
        }

        case 'repair_cycle_warning_review_completed': {
          if (cycleState.currentStage && cycleState.stages[cycleState.currentStage]) {
            const stage = cycleState.stages[cycleState.currentStage]
            const iteration = getCurrentIteration(stage)
            if (iteration) {
              iteration.fixCycle.warningsReviewed = (iteration.fixCycle.warningsReviewed || 0) + (data.warning_count || 0)
              // Store reviewed warnings if available
              if (data.warnings) {
                 iteration.fixCycle.warningsReviewedList = data.warnings
              }
            }
          }
          break
        }

        case 'repair_cycle_test_cycle_completed': {
          const completedTestType = data.test_type
          if (cycleState.stages[completedTestType]) {
            const stage = cycleState.stages[completedTestType]
            stage.status = data.passed ? 'completed' : 'failed'
            stage.duration = data.duration_seconds
            stage.endTime = timestamp
            stage.error = data.error
            
            // Ensure the last iteration status is cleared if it was stuck
            const iteration = getCurrentIteration(stage)
            if (iteration && iteration.fixCycle.status !== 'idle') {
               iteration.fixCycle.status = 'idle'
            }
          }
          break
        }

        case 'repair_cycle_completed':
        case 'agent_completed': // Fallback if repair_cycle_completed is not distinct
          if (data.agent_name === 'repair_cycle' || eventType === 'repair_cycle_completed') {
            cycleState.endTime = timestamp
            cycleState.overallSuccess = data.overall_success ?? data.success ?? data.passed ?? null
            cycleState.error = data.error
          }
          break
      }
    }

    return cycleState.isActive ? cycleState : null
  }, [events])

  if (!state) return null

  const formatDuration = (seconds) => {
    if (!seconds) return '-'
    const mins = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${mins}m ${secs}s`
  }

  const getStageIcon = (stage) => {
    if (stage.status === 'running') return <Loader2 className="w-4 h-4 animate-spin text-blue-500" />
    if (stage.status === 'completed') return <CheckCircle2 className="w-4 h-4 text-green-500" />
    if (stage.status === 'failed') return <XCircle className="w-4 h-4 text-red-500" />
    return <PlayCircle className="w-4 h-4 text-gray-400" />
  }

  return (
    <div className="bg-gh-canvas-subtle rounded-md border border-gh-border mb-5 overflow-hidden">
      <div className="p-3 border-b border-gh-border flex items-center justify-between bg-gh-canvas">
        <div className="flex items-center gap-2">
          <Wrench className="w-4 h-4 text-gh-fg-muted" />
          <h3 className="text-sm font-semibold text-gh-fg">Repair Cycle Status</h3>
        </div>
        {state.overallSuccess !== null && (
          <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
            state.overallSuccess ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
          }`}>
            {state.overallSuccess ? 'Success' : 'Failed'}
          </span>
        )}
      </div>

      <div className="divide-y divide-gh-border">
        {state.stageOrder.map(testType => {
          const stage = state.stages[testType]
          const isRunning = stage.status === 'running'
          
          return (
            <div key={testType} className={`p-3 ${isRunning ? 'bg-blue-50/50 dark:bg-blue-900/10' : ''}`}>
              {/* Stage Header */}
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  {getStageIcon(stage)}
                  <span className="font-medium text-sm capitalize">{testType} Tests</span>
                </div>
                <div className="flex items-center gap-3 text-xs text-gh-fg-muted">
                  {stage.duration && (
                    <span className="flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {formatDuration(stage.duration)}
                    </span>
                  )}
                </div>
              </div>

              {/* Iterations History */}
              <div className="pl-6 space-y-3">
                {stage.history.map((iteration, idx) => (
                  <div key={idx} className="relative border-l-2 border-gh-border pl-4 pb-1 last:pb-0">
                    <div className="absolute -left-[5px] top-0 w-2 h-2 rounded-full bg-gh-border"></div>
                    
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs font-semibold text-gh-fg">
                        Iteration {iteration.number}/{stage.maxIterations}
                      </span>
                    </div>

                    {/* Test Results */}
                    {iteration.results && (
                      <div className="flex flex-col gap-1 mb-1">
                        <div className="flex gap-2 flex-wrap">
                          <div className={`text-xs px-2 py-0.5 rounded border ${
                            iteration.results.failed > 0 
                              ? 'bg-red-50 border-red-200 text-red-700 dark:bg-red-900/20 dark:border-red-800 dark:text-red-400' 
                              : 'bg-green-50 border-green-200 text-green-700 dark:bg-green-900/20 dark:border-green-800 dark:text-green-400'
                          }`}>
                            <span className="font-semibold">{iteration.results.failed}</span> failed
                          </div>
                          <div className="text-xs px-2 py-0.5 rounded border bg-gh-canvas border-gh-border text-gh-fg-muted">
                            <span className="font-semibold text-gh-fg">{iteration.results.passed}</span> passed
                          </div>
                          {iteration.results.warnings > 0 && (
                            <div className="text-xs px-2 py-0.5 rounded border bg-yellow-50 border-yellow-200 text-yellow-700 dark:bg-yellow-900/20 dark:border-yellow-800 dark:text-yellow-400 flex items-center gap-1">
                              <AlertTriangle className="w-3 h-3" />
                              <span className="font-semibold">{iteration.results.warnings}</span> warnings
                            </div>
                          )}
                        </div>

                        {/* Failures List */}
                        {iteration.results.failures && iteration.results.failures.length > 0 && (
                          <details className="mt-1 text-xs group">
                            <summary className="cursor-pointer text-red-600 dark:text-red-400 font-medium hover:underline flex items-center gap-1 select-none">
                              <span className="group-open:rotate-90 transition-transform text-[10px]">▶</span>
                              Show {iteration.results.failures.length} failures
                            </summary>
                            <div className="mt-1 pl-2 border-l-2 border-red-200 dark:border-red-800 space-y-1 max-h-40 overflow-y-auto">
                              {iteration.results.failures.map((fail, fIdx) => (
                                <div key={fIdx} className="font-mono text-[10px] text-gh-fg-muted break-all">
                                  <span className="font-semibold text-red-600 dark:text-red-400">{fail.file}</span>
                                  <span className="mx-1 text-gh-fg-subtle">::</span>
                                  <span>{fail.test}</span>
                                </div>
                              ))}
                            </div>
                          </details>
                        )}
                      </div>
                    )}

                    {/* Fix Cycle Status */}
                    {(iteration.fixCycle.status !== 'idle' || iteration.fixCycle.filesFixed > 0 || iteration.fixCycle.warningsReviewed > 0) && (
                      <div className="flex flex-col gap-1 mt-1">
                        {/* File Fixes */}
                        {(iteration.fixCycle.filesToFix > 0 || iteration.fixCycle.filesFixed > 0) && (
                          <div className="text-xs flex items-center gap-2">
                            <Wrench className="w-3 h-3 text-gh-fg-muted" />
                            {iteration.fixCycle.status === 'fixing' ? (
                              <span className="text-blue-600 dark:text-blue-400 animate-pulse">
                                Fixing {iteration.fixCycle.filesFixed}/{iteration.fixCycle.filesToFix} files...
                              </span>
                            ) : (
                              <span className="text-gh-fg-muted">
                                Fixed {iteration.fixCycle.filesFixed} files
                              </span>
                            )}
                          </div>
                        )}
                        
                        {/* Warning Fixes */}
                        {(iteration.results?.warnings > 0 || iteration.fixCycle.warningsReviewed > 0) && (
                          <div className="flex flex-col">
                            <div className="text-xs flex items-center gap-2">
                              <AlertTriangle className="w-3 h-3 text-gh-fg-muted" />
                              {iteration.fixCycle.status === 'reviewing_warnings' ? (
                                <span className="text-yellow-600 dark:text-yellow-400 animate-pulse">
                                  Reviewing warnings ({iteration.fixCycle.warningsReviewed}/{iteration.results?.warnings || '?'})...
                                </span>
                              ) : iteration.fixCycle.warningsReviewed > 0 ? (
                                <span className="text-gh-fg-muted">
                                  Reviewed {iteration.fixCycle.warningsReviewed} warnings
                                </span>
                              ) : (
                                <span className="text-gh-fg-muted">
                                  {iteration.results?.warnings} warnings detected
                                </span>
                              )}
                            </div>

                            {/* Warnings List */}
                            {iteration.fixCycle.warnings && iteration.fixCycle.warnings.length > 0 && (
                               <details className="mt-1 ml-5 text-xs group">
                                <summary className="cursor-pointer text-yellow-600 dark:text-yellow-400 font-medium hover:underline flex items-center gap-1 select-none">
                                  <span className="group-open:rotate-90 transition-transform text-[10px]">▶</span>
                                  Show warnings
                                </summary>
                                <div className="mt-1 pl-2 border-l-2 border-yellow-200 dark:border-yellow-800 space-y-1 max-h-40 overflow-y-auto">
                                  {iteration.fixCycle.warnings.map((warn, wIdx) => (
                                    <div key={wIdx} className="font-mono text-[10px] text-gh-fg-muted break-all">
                                      <span className="font-semibold text-yellow-600 dark:text-yellow-400">{warn.file}</span>
                                      <div className="pl-2 text-gh-fg-subtle">{warn.message}</div>
                                    </div>
                                  ))}
                                </div>
                              </details>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              
              {/* Error Message */}
              {stage.error && (
                <div className="pl-6 mt-2 text-xs text-red-600 dark:text-red-400">
                  Error: {stage.error}
                </div>
              )}
            </div>
          )
        })}
        
        {state.stageOrder.length === 0 && (
          <div className="p-4 text-center text-sm text-gh-fg-muted">
            Initializing repair cycle...
          </div>
        )}
      </div>
    </div>
  )
}