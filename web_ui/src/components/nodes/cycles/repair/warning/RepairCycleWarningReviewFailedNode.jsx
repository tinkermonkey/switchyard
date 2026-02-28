import { XCircle } from 'lucide-react'
import RepairCycleWarningReviewNode from './RepairCycleWarningReviewNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function RepairCycleWarningReviewFailedNode({ data }) {
  return (
    <RepairCycleWarningReviewNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<XCircle className="w-4 h-4" />}
    />
  )
}
