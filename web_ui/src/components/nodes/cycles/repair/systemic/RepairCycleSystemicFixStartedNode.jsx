import { Wrench } from 'lucide-react'
import RepairCycleSystemicNode from './RepairCycleSystemicNode'

export default function RepairCycleSystemicFixStartedNode({ data }) {
  return <RepairCycleSystemicNode data={data} icon={<Wrench className="w-4 h-4" />} />
}
