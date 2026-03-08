import { memo } from 'react'
import { Box } from 'lucide-react'
import { CycleContainer } from './CycleContainer'
import { CYCLE_THEME_MAP } from '../../utils/cycleThemes.js'

/**
 * Unified React Flow node component for all container node types.
 * Dispatches on data.cycleType → looks up theme from CYCLE_THEME_MAP →
 * renders CycleContainer with that theme.
 *
 * Registered in index.js for every container node type string:
 *   reviewCycleContainer, repairCycleContainer, prReviewCycleContainer,
 *   conversationalLoopContainer, iterationContainer, subCycleContainer
 */

const DEFAULT_THEME = {
  borderColor:         '#6b7280',
  borderStyle:         'dashed',
  bgColor:             'rgba(107,114,128,0.06)',
  cornerColor:         'rgba(107,114,128,0.35)',
  icon:                Box,
  countSuffix:         'event',
  collapsedLabel:      'Container',
  collapsedTextColor:  '#d1d5db',
  collapsedCountColor: '#f3f4f6',
}

const CycleContainerNode = ({ data, ...props }) => {
  const theme = CYCLE_THEME_MAP[data.cycleType] ?? DEFAULT_THEME
  return <CycleContainer data={data} {...props} theme={theme} />
}

export default memo(CycleContainerNode)
