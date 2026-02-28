import { CheckCircle } from 'lucide-react'
import RepairCycleEventNode from './RepairCycleEventNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function RepairCycleCompletedNode({ data }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
