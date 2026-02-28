import { GitBranch } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#84cc16', borderColor: '#65a30d', color: '#fff' }

export default function BranchManagementNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <GitBranch className="w-4 h-4" />}
    />
  )
}
