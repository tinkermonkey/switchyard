import { PlayCircle } from 'lucide-react'
import PRReviewNode from './PRReviewNode'

export default function PRReviewStageStartedNode({ data }) {
  return <PRReviewNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
