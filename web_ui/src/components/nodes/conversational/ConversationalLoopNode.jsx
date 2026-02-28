import { MessageCircle } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#ec4899', borderColor: '#db2777', color: '#fff' }

export default function ConversationalLoopNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <MessageCircle className="w-4 h-4" />}
    />
  )
}
