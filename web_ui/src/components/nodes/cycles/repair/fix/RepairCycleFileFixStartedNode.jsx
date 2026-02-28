import { FileCode } from 'lucide-react'
import RepairCycleFixCycleNode from './RepairCycleFixCycleNode'

export default function RepairCycleFileFixStartedNode({ data }) {
  return <RepairCycleFixCycleNode data={data} icon={<FileCode className="w-4 h-4" />} />
}
