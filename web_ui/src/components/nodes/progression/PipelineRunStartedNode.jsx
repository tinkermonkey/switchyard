import { PlayCircle } from 'lucide-react'
import ProgressionNode from './ProgressionNode'

export default function PipelineRunStartedNode({ data }) {
  return <ProgressionNode data={data} icon={<PlayCircle className="w-4 h-4" />} />
}
