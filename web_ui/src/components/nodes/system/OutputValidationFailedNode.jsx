import { AlertCircle } from 'lucide-react'
import SystemOperationsNode from './SystemOperationsNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function OutputValidationFailedNode({ data }) {
  return (
    <SystemOperationsNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<AlertCircle className="w-4 h-4" />}
    />
  )
}
