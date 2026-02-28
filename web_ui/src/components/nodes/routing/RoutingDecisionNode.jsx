import { GitBranch } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#3b82f6', borderColor: '#2563eb', color: '#fff' }

export default function RoutingDecisionNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <GitBranch className="w-4 h-4" />}
    />
  )
}
