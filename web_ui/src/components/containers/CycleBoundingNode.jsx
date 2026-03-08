import { memo } from 'react'
import { RotateCcw, AlertTriangle, MessageSquare, Box } from 'lucide-react'
import { CycleContainer } from './CycleContainer'

const CYCLE_BOUNDING_THEME_MAP = {
  review_cycle: {
    borderColor:         '#9333ea',
    bgColor:             'rgba(147,51,234,0.05)',
    cornerColor:         'rgba(147,51,234,0.4)',
    icon:                RotateCcw,
    countSuffix:         'iteration',
    collapsedLabel:      'Review Cycle',
    collapsedTextColor:  '#c4b5fd',
    collapsedCountColor: '#e9d5ff',
  },
  error_handling: {
    borderColor:         '#dc2626',
    bgColor:             'rgba(220,38,38,0.05)',
    cornerColor:         'rgba(220,38,38,0.4)',
    icon:                AlertTriangle,
    countSuffix:         'iteration',
    collapsedLabel:      'Error Handling',
    collapsedTextColor:  '#fca5a5',
    collapsedCountColor: '#fee2e2',
  },
  conversational_loop: {
    borderColor:         '#2563eb',
    bgColor:             'rgba(37,99,235,0.05)',
    cornerColor:         'rgba(37,99,235,0.4)',
    icon:                MessageSquare,
    countSuffix:         'iteration',
    collapsedLabel:      'Conversational Loop',
    collapsedTextColor:  '#93c5fd',
    collapsedCountColor: '#dbeafe',
  },
  unknown: {
    borderColor:         '#6b7280',
    bgColor:             'rgba(107,114,128,0.05)',
    cornerColor:         'rgba(107,114,128,0.4)',
    icon:                Box,
    countSuffix:         'iteration',
    collapsedLabel:      'Cycle',
    collapsedTextColor:  '#d1d5db',
    collapsedCountColor: '#f3f4f6',
  },
}

const CycleBoundingNode = ({ data, ...props }) => {
  const theme = CYCLE_BOUNDING_THEME_MAP[data.cycleType] || CYCLE_BOUNDING_THEME_MAP.unknown
  return <CycleContainer data={data} {...props} theme={theme} />
}

export default memo(CycleBoundingNode)
