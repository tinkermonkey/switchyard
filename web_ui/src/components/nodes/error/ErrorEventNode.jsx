import { AlertCircle } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function ErrorEventNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <AlertCircle className="w-4 h-4" />}
    />
  )
}
