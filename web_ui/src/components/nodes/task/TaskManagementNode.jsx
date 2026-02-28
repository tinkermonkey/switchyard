import { ListTodo } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#06b6d4', borderColor: '#0891b2', color: '#fff' }

export default function TaskManagementNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <ListTodo className="w-4 h-4" />}
    />
  )
}
