import { ArrowUpCircle } from 'lucide-react'
import BranchManagementNode from './BranchManagementNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function BranchSelectionEscalatedNode({ data }) {
  return (
    <BranchManagementNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<ArrowUpCircle className="w-4 h-4" />}
    />
  )
}
