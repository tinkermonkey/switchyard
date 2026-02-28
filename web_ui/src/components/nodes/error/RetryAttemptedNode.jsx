import { RefreshCw } from 'lucide-react'
import ErrorEventNode from './ErrorEventNode'

const STYLE_OVERRIDE = { background: '#f59e0b', borderColor: '#d97706', color: '#fff' }

export default function RetryAttemptedNode({ data }) {
  return (
    <ErrorEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<RefreshCw className="w-4 h-4" />}
    />
  )
}
