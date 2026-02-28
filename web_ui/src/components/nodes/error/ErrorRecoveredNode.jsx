import { CheckCircle } from 'lucide-react'
import ErrorEventNode from './ErrorEventNode'

const STYLE_OVERRIDE = { background: '#10b981', borderColor: '#059669', color: '#fff' }

export default function ErrorRecoveredNode({ data }) {
  return (
    <ErrorEventNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<CheckCircle className="w-4 h-4" />}
    />
  )
}
