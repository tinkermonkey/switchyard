import { CheckCircle, XCircle } from 'lucide-react'
import PipelineLifecycleNode from './PipelineLifecycleNode'

const DEFAULT_STYLE = { background: '#6366f1', borderColor: '#4f46e5', color: '#fff' }
const FAILED_STYLE = { background: '#f85149', borderColor: '#da3633', color: '#fff' }

export default function PipelineCompletedNode({ data }) {
  const failed = data?.failed === true
  return (
    <PipelineLifecycleNode
      data={data}
      nodeStyle={failed ? FAILED_STYLE : DEFAULT_STYLE}
      icon={failed ? <XCircle className="w-4 h-4" /> : <CheckCircle className="w-4 h-4" />}
    />
  )
}
