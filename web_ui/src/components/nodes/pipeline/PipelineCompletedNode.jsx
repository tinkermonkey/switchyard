import { CheckCircle } from 'lucide-react'
import PipelineLifecycleNode from './PipelineLifecycleNode'

const DEFAULT_STYLE = { background: '#6366f1', borderColor: '#4f46e5', color: '#fff' }

export default function PipelineCompletedNode({ data }) {
  return (
    <PipelineLifecycleNode
      data={data}
      nodeStyle={DEFAULT_STYLE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
