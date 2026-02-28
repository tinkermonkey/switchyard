import { Box } from 'lucide-react'
import RepairCycleEventNode from '../RepairCycleEventNode'

const DEFAULT_STYLE = { background: '#374151', borderColor: '#4b5563', color: '#fff' }

export default function RepairCycleContainerEventNode({ data, nodeStyle, icon }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <Box className="w-4 h-4" />}
    />
  )
}
