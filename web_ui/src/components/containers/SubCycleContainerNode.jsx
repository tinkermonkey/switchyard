import { memo } from 'react'
import { Wrench, FlaskConical, AlertTriangle, Search, Box } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

/**
 * Themed wrapper for Level-3 sub-cycle containers inside repair cycle test cycles.
 * Uses CycleContainerNode as base. Collapsed by default; onToggleCollapse injected by caller.
 * Theme is selected by data.cycleType, set by buildFlowchart.js via eventProcessing.
 *
 * cycleType values: 'test_execution' | 'fix_cycle' | 'warning_review' | 'systemic_analysis' | 'systemic_fix'
 */

const SUB_CYCLE_THEME_MAP = {
  test_execution: {
    borderColor:         '#10b981',
    borderStyle:         'solid',
    bgColor:             'rgba(16,185,129,0.06)',
    cornerColor:         'rgba(16,185,129,0.35)',
    icon:                FlaskConical,
    countSuffix:         'event',
    collapsedLabel:      'Test Execution',
    collapsedTextColor:  '#6ee7b7',
    collapsedCountColor: '#d1fae5',
  },
  fix_cycle: {
    borderColor:         '#d97706',
    borderStyle:         'solid',
    bgColor:             'rgba(217,119,6,0.06)',
    cornerColor:         'rgba(217,119,6,0.35)',
    icon:                Wrench,
    countSuffix:         'fix',
    collapsedLabel:      'Fix Cycle',
    collapsedTextColor:  '#fcd34d',
    collapsedCountColor: '#fef3c7',
  },
  warning_review: {
    borderColor:         '#f59e0b',
    borderStyle:         'solid',
    bgColor:             'rgba(245,158,11,0.06)',
    cornerColor:         'rgba(245,158,11,0.35)',
    icon:                AlertTriangle,
    countSuffix:         'warning',
    collapsedLabel:      'Warning Review',
    collapsedTextColor:  '#fde68a',
    collapsedCountColor: '#fef3c7',
  },
  systemic_analysis: {
    borderColor:         '#8b5cf6',
    borderStyle:         'solid',
    bgColor:             'rgba(139,92,246,0.06)',
    cornerColor:         'rgba(139,92,246,0.35)',
    icon:                Search,
    countSuffix:         'event',
    collapsedLabel:      'Systemic Analysis',
    collapsedTextColor:  '#c4b5fd',
    collapsedCountColor: '#ede9fe',
  },
  systemic_fix: {
    borderColor:         '#a78bfa',
    borderStyle:         'solid',
    bgColor:             'rgba(167,139,250,0.06)',
    cornerColor:         'rgba(167,139,250,0.35)',
    icon:                Wrench,
    countSuffix:         'fix',
    collapsedLabel:      'Systemic Fix',
    collapsedTextColor:  '#ddd6fe',
    collapsedCountColor: '#ede9fe',
  },
  default: {
    borderColor:         '#6b7280',
    borderStyle:         'solid',
    bgColor:             'rgba(107,114,128,0.06)',
    cornerColor:         'rgba(107,114,128,0.35)',
    icon:                Box,
    countSuffix:         'event',
    collapsedLabel:      'Sub-cycle',
    collapsedTextColor:  '#d1d5db',
    collapsedCountColor: '#f3f4f6',
  },
}

const SubCycleContainerNode = ({ data, ...props }) => {
  const theme = SUB_CYCLE_THEME_MAP[data.cycleType] || SUB_CYCLE_THEME_MAP.default
  return <CycleContainerNode data={data} {...props} theme={theme} />
}

export default memo(SubCycleContainerNode)
