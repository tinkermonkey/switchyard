import { CircleCheck } from 'lucide-react'
import AgentLifecycleNode from './AgentLifecycleNode'

export default function AgentCompletedNode({ data }) {
  return <AgentLifecycleNode data={data} icon={<CircleCheck className="w-4 h-4" />} />
}
