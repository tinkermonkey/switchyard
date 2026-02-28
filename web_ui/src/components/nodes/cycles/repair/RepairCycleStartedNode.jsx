import { PlayCircle } from 'lucide-react'
import RepairCycleEventNode from './RepairCycleEventNode'

export default function RepairCycleStartedNode({ data }) {
  return <RepairCycleEventNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
