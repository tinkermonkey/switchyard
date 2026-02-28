import { Settings } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#374151', borderColor: '#4b5563', color: '#fff' }

export default function SystemOperationsNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <Settings className="w-4 h-4" />}
    />
  )
}
