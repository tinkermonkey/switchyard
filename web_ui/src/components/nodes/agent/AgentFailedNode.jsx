import { CircleX } from 'lucide-react'
import AgentLifecycleNode from './AgentLifecycleNode'

export default function AgentFailedNode({ data }) {
  return <AgentLifecycleNode data={data} icon={<CircleX className="w-4 h-4" />} />
}
