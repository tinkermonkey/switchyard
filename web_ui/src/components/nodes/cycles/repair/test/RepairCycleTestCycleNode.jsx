import { FlaskConical } from 'lucide-react'
import RepairCycleEventNode from '../RepairCycleEventNode'

const DEFAULT_STYLE = { background: '#f59e0b', borderColor: '#d97706', color: '#fff' }

export default function RepairCycleTestCycleNode({ data, nodeStyle, icon }) {
  return (
    <RepairCycleEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <FlaskConical className="w-4 h-4" />}
    />
  )
}
