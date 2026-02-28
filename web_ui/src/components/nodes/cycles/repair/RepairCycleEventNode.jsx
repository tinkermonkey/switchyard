import { Wrench } from 'lucide-react'
import CycleEventNode from '../CycleEventNode'

const DEFAULT_STYLE = { background: '#f97316', borderColor: '#ea580c', color: '#fff' }

export default function RepairCycleEventNode({ data, nodeStyle, icon }) {
  return (
    <CycleEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <Wrench className="w-4 h-4" />}
    />
  )
}
