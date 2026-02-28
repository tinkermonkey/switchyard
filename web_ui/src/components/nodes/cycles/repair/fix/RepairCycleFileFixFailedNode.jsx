import { FileCode } from 'lucide-react'
import RepairCycleFixCycleNode from './RepairCycleFixCycleNode'

const STYLE_OVERRIDE = { background: '#7f1d1d', borderColor: '#991b1b', color: '#fff' }

export default function RepairCycleFileFixFailedNode({ data }) {
  return (
    <RepairCycleFixCycleNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<FileCode className="w-4 h-4" />}
    />
  )
}
