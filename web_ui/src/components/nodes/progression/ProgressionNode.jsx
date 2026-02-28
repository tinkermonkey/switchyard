import { ArrowRight } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function ProgressionNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <ArrowRight className="w-4 h-4" />}
    />
  )
}
