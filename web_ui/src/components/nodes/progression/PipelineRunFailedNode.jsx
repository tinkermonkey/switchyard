import { XCircle } from 'lucide-react'
import ProgressionNode from './ProgressionNode'

const STYLE_OVERRIDE = { background: '#ef4444', borderColor: '#dc2626', color: '#fff' }

export default function PipelineRunFailedNode({ data }) {
  return (
    <ProgressionNode
      data={data}
      nodeStyle={STYLE_OVERRIDE}
      icon={<XCircle className="w-4 h-4" />}
    />
  )
}
