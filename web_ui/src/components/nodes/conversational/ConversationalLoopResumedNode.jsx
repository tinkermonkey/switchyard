import { Play } from 'lucide-react'
import ConversationalLoopNode from './ConversationalLoopNode'

export default function ConversationalLoopResumedNode({ data }) {
  return <ConversationalLoopNode data={data} icon={<Play className="w-4 h-4" />} />
}
