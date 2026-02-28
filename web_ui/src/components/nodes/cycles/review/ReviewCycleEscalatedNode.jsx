import { AlertTriangle } from 'lucide-react'
import ReviewCycleEventNode from './ReviewCycleEventNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function ReviewCycleEscalatedNode({ data }) {
  return (
    <ReviewCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<AlertTriangle className="w-4 h-4" />}
    />
  )
}
