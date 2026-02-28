import { RefreshCw } from 'lucide-react'
import SystemOperationsNode from './SystemOperationsNode'

export default function ExecutionStateReconciledNode({ data }) {
  return <SystemOperationsNode data={data} icon={<RefreshCw className="w-4 h-4" />} />
}
