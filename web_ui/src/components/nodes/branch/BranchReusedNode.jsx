import { GitBranch } from 'lucide-react'
import BranchManagementNode from './BranchManagementNode'

export default function BranchReusedNode({ data }) {
  return <BranchManagementNode data={data} icon={<GitBranch className="w-4 h-4" />} />
}
