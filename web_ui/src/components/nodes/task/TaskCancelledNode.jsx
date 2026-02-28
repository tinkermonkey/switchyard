import { X } from 'lucide-react'
import TaskManagementNode from './TaskManagementNode'

const STYLE_OVERRIDE = { background: '#6e7681', borderColor: '#4b5563', color: '#fff' }

export default function TaskCancelledNode({ data }) {
  return (
    <TaskManagementNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<X className="w-4 h-4" />}
    />
  )
}
