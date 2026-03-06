import { memo } from 'react'
import { MessageSquare } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

const CONV_LOOP_THEME = {
  borderColor:         '#ec4899',
  bgColor:             'rgba(236,72,153,0.12)',
  cornerColor:         'rgba(236,72,153,0.5)',
  icon:                MessageSquare,
  countSuffix:         'question',
  collapsedLabel:      'Conversation',
  collapsedTextColor:  '#f9a8d4',
  collapsedCountColor: '#fce7f3',
}

const ConversationalLoopContainerNode = props => <CycleContainerNode {...props} theme={CONV_LOOP_THEME} />

export default memo(ConversationalLoopContainerNode)
