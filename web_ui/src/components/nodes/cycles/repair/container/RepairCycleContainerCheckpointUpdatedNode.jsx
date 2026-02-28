import { Save } from 'lucide-react'
import RepairCycleContainerEventNode from './RepairCycleContainerEventNode'

export default function RepairCycleContainerCheckpointUpdatedNode({ data }) {
  return <RepairCycleContainerEventNode data={data} icon={<Save className="w-4 h-4" />} />
}
