import { CheckCircle } from 'lucide-react'
import RepairCycleSystemicNode from './RepairCycleSystemicNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function RepairCycleSystemicAnalysisCompletedNode({ data }) {
  return (
    <RepairCycleSystemicNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
