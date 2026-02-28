import { FileCode } from 'lucide-react'
import RepairCycleFixCycleNode from './RepairCycleFixCycleNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function RepairCycleFileFixCompletedNode({ data }) {
  return (
    <RepairCycleFixCycleNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<FileCode className="w-4 h-4" />}
    />
  )
}
