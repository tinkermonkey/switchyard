import { Pause } from 'lucide-react'
import ConversationalLoopNode from './ConversationalLoopNode'

export default function ConversationalLoopPausedNode({ data }) {
  return <ConversationalLoopNode data={data} icon={<Pause className="w-4 h-4" />} />
}
