import { UserCheck } from 'lucide-react'
import ReviewCycleEventNode from './ReviewCycleEventNode'

const STYLE_OVERRIDE = { background: '#6d28d9', borderColor: '#5b21b6', color: '#fff' }

export default function ReviewCycleReviewerSelectedNode({ data }) {
  return (
    <ReviewCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<UserCheck className="w-4 h-4" />}
    />
  )
}
