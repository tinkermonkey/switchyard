import { Radio } from 'lucide-react'
import FeedbackNode from './FeedbackNode'

export default function FeedbackListeningStartedNode({ data }) {
  return <FeedbackNode data={data} icon={<Radio className="w-4 h-4" />} />
}
