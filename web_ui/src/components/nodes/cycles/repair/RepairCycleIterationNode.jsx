import { RefreshCw } from 'lucide-react'
import RepairCycleEventNode from './RepairCycleEventNode'

export default function RepairCycleIterationNode({ data }) {
  return <RepairCycleEventNode data={data} icon={<RefreshCw className="w-4 h-4" />} />
}
