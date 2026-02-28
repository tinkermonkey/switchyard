import { XCircle } from 'lucide-react'
import IssueManagementNode from './IssueManagementNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function SubIssueCreationFailedNode({ data }) {
  return (
    <IssueManagementNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<XCircle className="w-4 h-4" />}
    />
  )
}
