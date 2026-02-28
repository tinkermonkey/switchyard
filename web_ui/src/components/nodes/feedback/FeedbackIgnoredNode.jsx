import { BellOff } from 'lucide-react'
import FeedbackNode from './FeedbackNode'

const STYLE_OVERRIDE = { background: '#6e7681', borderColor: '#4b5563', color: '#fff' }

export default function FeedbackIgnoredNode({ data }) {
  return (
    <FeedbackNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<BellOff className="w-4 h-4" />}
    />
  )
}
