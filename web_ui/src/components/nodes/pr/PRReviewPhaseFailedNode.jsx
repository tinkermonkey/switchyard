import { XCircle } from 'lucide-react'
import PRReviewNode from './PRReviewNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function PRReviewPhaseFailedNode({ data }) {
  return (
    <PRReviewNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<XCircle className="w-4 h-4" />}
    />
  )
}
