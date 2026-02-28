import { CheckCircle } from 'lucide-react'
import RepairCycleTestCycleNode from './RepairCycleTestCycleNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function RepairCycleTestCycleCompletedNode({ data }) {
  return (
    <RepairCycleTestCycleNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
