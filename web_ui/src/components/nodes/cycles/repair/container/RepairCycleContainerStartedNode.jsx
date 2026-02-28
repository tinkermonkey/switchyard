import { PlayCircle } from 'lucide-react'
import RepairCycleContainerEventNode from './RepairCycleContainerEventNode'

export default function RepairCycleContainerStartedNode({ data }) {
  return <RepairCycleContainerEventNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
