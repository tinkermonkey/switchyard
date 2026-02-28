import { CheckCircle } from 'lucide-react'
import RepairCycleEventNode from './RepairCycleEventNode'

const STYLE_OVERRIDE = { background: '#06b6d4', borderColor: '#0891b2', color: '#fff' }

export default function RepairCycleEnvRebuildCompletedNode({ data }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
