import { PlayCircle } from 'lucide-react'
import RepairCycleTestCycleNode from './RepairCycleTestCycleNode'

export default function RepairCycleTestCycleStartedNode({ data }) {
  return <RepairCycleTestCycleNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
