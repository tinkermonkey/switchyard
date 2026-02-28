import { PlayCircle } from 'lucide-react'
import ReviewCycleEventNode from './ReviewCycleEventNode'

export default function ReviewCycleStartedNode({ data }) {
  return <ReviewCycleEventNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
