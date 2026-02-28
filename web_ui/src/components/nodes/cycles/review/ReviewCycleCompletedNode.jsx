import { CheckCircle } from 'lucide-react'
import ReviewCycleEventNode from './ReviewCycleEventNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function ReviewCycleCompletedNode({ data }) {
  return (
    <ReviewCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
