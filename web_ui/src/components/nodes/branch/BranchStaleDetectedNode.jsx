import { Clock } from 'lucide-react'
import BranchManagementNode from './BranchManagementNode'

const STYLE_OVERRIDE = { background: '#f59e0b', borderColor: '#d97706', color: '#fff' }

export default function BranchStaleDetectedNode({ data }) {
  return (
    <BranchManagementNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<Clock className="w-4 h-4" />}
    />
  )
}
