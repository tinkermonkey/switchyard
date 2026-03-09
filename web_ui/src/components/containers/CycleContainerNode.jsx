import { memo } from 'react'
import { Box } from 'lucide-react'
import { CycleContainer } from './CycleContainer'
import { CYCLE_THEME_MAP } from '../../utils/cycleThemes.js'
import { useTheme } from '../../contexts/ThemeContext.jsx'

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
  borderColor:              '#6b7280',
  borderStyle:              'dashed',
  bgColor:                  'rgba(107,114,128,0.06)',
  cornerColor:              'rgba(107,114,128,0.35)',
  icon:                     Box,
  countSuffix:              'event',
  collapsedLabel:           'Container',
  collapsedTextColor:       '#d1d5db',
  collapsedCountColor:      '#9ca3af',
  collapsedCountColorLight: '#374151',
}

const FAILURE_THEME_OVERRIDE = {
  borderColor:              '#ef4444',
  bgColor:                  'rgba(239,68,68,0.10)',
  cornerColor:              'rgba(239,68,68,0.5)',
  collapsedTextColor:       '#fca5a5',
  collapsedCountColor:      '#fca5a5',
  collapsedCountColorLight: '#dc2626',
}

const CycleContainerNode = ({ data, ...props }) => {
  const { theme: appTheme } = useTheme()
  const isDark = appTheme === 'dark'
  const baseTheme = CYCLE_THEME_MAP[data.cycleType] ?? DEFAULT_THEME
  const theme = data.isFailure || data.summary?.isFailure
    ? { ...baseTheme, ...FAILURE_THEME_OVERRIDE }
    : baseTheme
  return <CycleContainer data={data} {...props} theme={theme} isDark={isDark} />
}

export default memo(CycleContainerNode)
