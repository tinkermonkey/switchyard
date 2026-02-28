import { MessageSquare } from 'lucide-react'
import FeedbackNode from './FeedbackNode'

export default function FeedbackDetectedNode({ data }) {
  return <FeedbackNode data={data} icon={<MessageSquare className="w-4 h-4" />} />
}
