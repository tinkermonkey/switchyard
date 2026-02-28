import { PlayCircle } from 'lucide-react'
import TaskManagementNode from './TaskManagementNode'

export default function TaskDequeuedNode({ data }) {
  return <TaskManagementNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
