import { MessageSquare } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#f59e0b', borderColor: '#d97706', color: '#fff' }

export default function FeedbackNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <MessageSquare className="w-4 h-4" />}
    />
  )
}
