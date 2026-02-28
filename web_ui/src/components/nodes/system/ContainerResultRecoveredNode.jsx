import { CheckCircle } from 'lucide-react'
import SystemOperationsNode from './SystemOperationsNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function ContainerResultRecoveredNode({ data }) {
  return (
    <SystemOperationsNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
