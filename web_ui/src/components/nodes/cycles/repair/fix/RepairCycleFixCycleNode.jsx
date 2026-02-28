import { Wrench } from 'lucide-react'
import RepairCycleEventNode from '../RepairCycleEventNode'

const DEFAULT_STYLE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function RepairCycleFixCycleNode({ data, nodeStyle, icon }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <Wrench className="w-4 h-4" />}
    />
  )
}
