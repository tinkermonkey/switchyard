import { Database } from 'lucide-react'
import SystemOperationsNode from './SystemOperationsNode'

const STYLE_OVERRIDE = { background: '#f59e0b', borderColor: '#d97706', color: '#fff' }

export default function FallbackStorageUsedNode({ data }) {
  return (
    <SystemOperationsNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<Database className="w-4 h-4" />}
    />
  )
}
