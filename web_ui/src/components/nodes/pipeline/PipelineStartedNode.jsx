import { PlayCircle } from 'lucide-react'
import PipelineLifecycleNode from './PipelineLifecycleNode'

const DEFAULT_STYLE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function PipelineStartedNode({ data }) {
  return (
    <PipelineLifecycleNode
      data={data}
      nodeStyle={DEFAULT_STYLE}
      icon={<PlayCircle className="w-4 h-4" />}
    />
  )
}
