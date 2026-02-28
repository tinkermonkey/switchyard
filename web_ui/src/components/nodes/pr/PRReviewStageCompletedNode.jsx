import { CheckCircle } from 'lucide-react'
import PRReviewNode from './PRReviewNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function PRReviewStageCompletedNode({ data }) {
  return (
    <PRReviewNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
