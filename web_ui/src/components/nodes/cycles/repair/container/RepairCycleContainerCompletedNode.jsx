import { CheckCircle } from 'lucide-react'
import RepairCycleContainerEventNode from './RepairCycleContainerEventNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function RepairCycleContainerCompletedNode({ data }) {
  return (
    <RepairCycleContainerEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
