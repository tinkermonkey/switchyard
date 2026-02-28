import { GitBranch } from 'lucide-react'
import RoutingDecisionNode from './RoutingDecisionNode'

export default function AgentRoutingDecisionNode({ data }) {
  return <RoutingDecisionNode data={data} icon={<GitBranch className="w-4 h-4" />} />
}
