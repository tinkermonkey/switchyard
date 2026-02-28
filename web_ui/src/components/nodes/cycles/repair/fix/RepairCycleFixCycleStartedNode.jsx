import { PlayCircle } from 'lucide-react'
import RepairCycleFixCycleNode from './RepairCycleFixCycleNode'

export default function RepairCycleFixCycleStartedNode({ data }) {
  return <RepairCycleFixCycleNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
