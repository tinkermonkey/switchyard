import { FolderOpen } from 'lucide-react'
import RoutingDecisionNode from './RoutingDecisionNode'

export default function WorkspaceRoutingNode({ data }) {
  return <RoutingDecisionNode data={data} icon={<FolderOpen className="w-4 h-4" />} />
}
