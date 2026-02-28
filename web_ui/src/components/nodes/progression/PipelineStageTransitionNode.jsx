import { ArrowRight } from 'lucide-react'
import ProgressionNode from './ProgressionNode'

export default function PipelineStageTransitionNode({ data }) {
  return <ProgressionNode data={data} icon={<ArrowRight className="w-4 h-4" />} />
}
