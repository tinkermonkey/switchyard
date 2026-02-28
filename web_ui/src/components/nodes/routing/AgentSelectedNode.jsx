import { User } from 'lucide-react'
import RoutingDecisionNode from './RoutingDecisionNode'

export default function AgentSelectedNode({ data }) {
  return <RoutingDecisionNode data={data} icon={<User className="w-4 h-4" />} />
}
