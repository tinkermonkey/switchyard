import { PlayCircle } from 'lucide-react'
import RepairCycleSystemicNode from './RepairCycleSystemicNode'

export default function RepairCycleSystemicAnalysisStartedNode({ data }) {
  return <RepairCycleSystemicNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
