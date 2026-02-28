import { ArrowUpCircle } from 'lucide-react'
import TaskManagementNode from './TaskManagementNode'

export default function TaskPriorityChangedNode({ data }) {
  return <TaskManagementNode data={data} icon={<ArrowUpCircle className="w-4 h-4" />} />
}
