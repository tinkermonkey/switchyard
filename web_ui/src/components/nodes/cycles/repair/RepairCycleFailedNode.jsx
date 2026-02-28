import { XCircle } from 'lucide-react'
import RepairCycleEventNode from './RepairCycleEventNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function RepairCycleFailedNode({ data }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<XCircle className="w-4 h-4" />}
    />
  )
}
