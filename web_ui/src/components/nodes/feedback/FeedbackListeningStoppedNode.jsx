import { VolumeX } from 'lucide-react'
import FeedbackNode from './FeedbackNode'

export default function FeedbackListeningStoppedNode({ data }) {
  return <FeedbackNode data={data} icon={<VolumeX className="w-4 h-4" />} />
}
