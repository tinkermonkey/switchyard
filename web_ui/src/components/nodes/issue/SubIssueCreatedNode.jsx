import { PlusCircle } from 'lucide-react'
import IssueManagementNode from './IssueManagementNode'

export default function SubIssueCreatedNode({ data }) {
  return <IssueManagementNode data={data} icon={<PlusCircle className="w-4 h-4" />} />
}
