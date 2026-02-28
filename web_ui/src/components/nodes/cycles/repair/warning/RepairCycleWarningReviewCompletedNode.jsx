import { CheckCircle } from 'lucide-react'
import RepairCycleWarningReviewNode from './RepairCycleWarningReviewNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function RepairCycleWarningReviewCompletedNode({ data }) {
  return (
    <RepairCycleWarningReviewNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
