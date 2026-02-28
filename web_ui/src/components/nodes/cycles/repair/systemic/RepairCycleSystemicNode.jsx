import { Search } from 'lucide-react'
import RepairCycleEventNode from '../RepairCycleEventNode'

const DEFAULT_STYLE = { background: '#8b5cf6', borderColor: '#7c3aed', color: '#fff' }

export default function RepairCycleSystemicNode({ data, nodeStyle, icon }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <Search className="w-4 h-4" />}
    />
  )
}
