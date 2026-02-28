import { CheckCircle } from 'lucide-react'
import ProgressionNode from './ProgressionNode'

export default function PipelineRunCompletedNode({ data }) {
  return <ProgressionNode data={data} icon={<CheckCircle className="w-4 h-4" />} />
}
