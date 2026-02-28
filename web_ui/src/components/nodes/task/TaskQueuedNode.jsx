import { Clock } from 'lucide-react'
import TaskManagementNode from './TaskManagementNode'

export default function TaskQueuedNode({ data }) {
  return <TaskManagementNode data={data} icon={<Clock className="w-4 h-4" />} />
}
