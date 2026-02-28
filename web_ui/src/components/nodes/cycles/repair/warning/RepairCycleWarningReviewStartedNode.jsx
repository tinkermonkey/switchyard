import { PlayCircle } from 'lucide-react'
import RepairCycleWarningReviewNode from './RepairCycleWarningReviewNode'

export default function RepairCycleWarningReviewStartedNode({ data }) {
  return <RepairCycleWarningReviewNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
