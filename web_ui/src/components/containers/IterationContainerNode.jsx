import { memo } from 'react'
import { RotateCcw, Wrench, GitPullRequest, Box } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

/**
 * Themed wrappers for Level-2 iteration/phase containers inside each top-level cycle.
 * Uses CycleContainerNode as base. Collapsed by default; onToggleCollapse injected by caller.
 * Theme is selected by data.cycleType, injected by buildFlowchart.js.
 *
 * cycleType values: 'review' | 'repair' | 'pr_review'
 */

const ITERATION_THEME_MAP = {
  review: {
    borderColor:         '#9333ea',
    borderStyle:         'solid',
    bgColor:             'rgba(147,51,234,0.06)',
    cornerColor:         'rgba(147,51,234,0.35)',
    icon:                RotateCcw,
    countSuffix:         'event',
    collapsedLabel:      'Iteration',
    collapsedTextColor:  '#c4b5fd',
    collapsedCountColor: '#e9d5ff',
  },
  repair: {
    borderColor:         '#d97706',
    borderStyle:         'solid',
    bgColor:             'rgba(217,119,6,0.06)',
    cornerColor:         'rgba(217,119,6,0.35)',
    icon:                Wrench,
    countSuffix:         'sub-cycle',
    collapsedLabel:      'Test Cycle',
    collapsedTextColor:  '#fcd34d',
    collapsedCountColor: '#fef3c7',
  },
  pr_review: {
    borderColor:         '#0ea5e9',
    borderStyle:         'solid',
    bgColor:             'rgba(14,165,233,0.06)',
    cornerColor:         'rgba(14,165,233,0.35)',
    icon:                GitPullRequest,
    countSuffix:         'event',
    collapsedLabel:      'Phase',
    collapsedTextColor:  '#7dd3fc',
    collapsedCountColor: '#e0f2fe',
  },
  default: {
    borderColor:         '#6366f1',
    borderStyle:         'solid',
    bgColor:             'rgba(99,102,241,0.06)',
    cornerColor:         'rgba(99,102,241,0.35)',
    icon:                Box,
    countSuffix:         'event',
    collapsedLabel:      'Iteration',
    collapsedTextColor:  '#a5b4fc',
    collapsedCountColor: '#e0e7ff',
  },
}

const IterationContainerNode = ({ data, ...props }) => {
  const theme = ITERATION_THEME_MAP[data.cycleType] || ITERATION_THEME_MAP.default
  return <CycleContainerNode data={data} {...props} theme={theme} />
}

export default memo(IterationContainerNode)
