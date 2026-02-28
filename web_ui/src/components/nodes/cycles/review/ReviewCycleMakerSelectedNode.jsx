import { User } from 'lucide-react'
import ReviewCycleEventNode from './ReviewCycleEventNode'

const STYLE_OVERRIDE = { background: '#7c3aed', borderColor: '#6d28d9', color: '#fff' }

export default function ReviewCycleMakerSelectedNode({ data }) {
  return (
    <ReviewCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<User className="w-4 h-4" />}
    />
  )
}
