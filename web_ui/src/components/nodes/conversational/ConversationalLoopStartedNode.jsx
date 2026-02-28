import { PlayCircle } from 'lucide-react'
import ConversationalLoopNode from './ConversationalLoopNode'

export default function ConversationalLoopStartedNode({ data }) {
  return <ConversationalLoopNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
