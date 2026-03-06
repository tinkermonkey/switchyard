import { memo } from 'react'
import { RotateCcw } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

const REVIEW_CYCLE_THEME = {
  borderColor:        '#9333ea',
  bgColor:            'rgba(147, 51, 234, 0.14)',
  cornerColor:        'rgba(147,51,234,0.6)',
  icon:               RotateCcw,
  countSuffix:        'iteration',
  collapsedLabel:     'Review Cycle',
  collapsedTextColor: '#c4b5fd',
  collapsedCountColor:'#e9d5ff',
}

const ReviewCycleContainerNode = props => <CycleContainerNode {...props} theme={REVIEW_CYCLE_THEME} />

export default memo(ReviewCycleContainerNode)
