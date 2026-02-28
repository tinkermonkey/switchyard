import { GitBranch } from 'lucide-react'
import CycleEventNode from '../CycleEventNode'

const DEFAULT_STYLE = { background: '#8b5cf6', borderColor: '#7c3aed', color: '#fff' }

export default function ReviewCycleEventNode({ data, nodeStyle, icon }) {
  return (
    <CycleEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <GitBranch className="w-4 h-4" />}
    />
  )
}
