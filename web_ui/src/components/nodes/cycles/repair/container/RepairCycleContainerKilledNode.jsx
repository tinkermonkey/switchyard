import { XCircle } from 'lucide-react'
import RepairCycleContainerEventNode from './RepairCycleContainerEventNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function RepairCycleContainerKilledNode({ data }) {
  return (
    <RepairCycleContainerEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<XCircle className="w-4 h-4" />}
    />
  )
}
