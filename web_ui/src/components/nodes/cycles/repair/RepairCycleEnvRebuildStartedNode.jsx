import { RefreshCw } from 'lucide-react'
import RepairCycleEventNode from './RepairCycleEventNode'

const STYLE_OVERRIDE = { background: '#06b6d4', borderColor: '#0891b2', color: '#fff' }

export default function RepairCycleEnvRebuildStartedNode({ data }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<RefreshCw className="w-4 h-4" />}
    />
  )
}
