import { CheckCircle } from 'lucide-react'
import RepairCycleFixCycleNode from './RepairCycleFixCycleNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function RepairCycleFixCycleCompletedNode({ data }) {
  return (
    <RepairCycleFixCycleNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
