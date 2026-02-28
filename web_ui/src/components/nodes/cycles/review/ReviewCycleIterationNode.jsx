import { RefreshCw } from 'lucide-react'
import ReviewCycleEventNode from './ReviewCycleEventNode'

export default function ReviewCycleIterationNode({ data }) {
  return <ReviewCycleEventNode data={data} icon={<RefreshCw className="w-4 h-4" />} />
}
