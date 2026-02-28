import { GitPullRequest } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const DEFAULT_STYLE = { background: '#6366f1', borderColor: '#4f46e5', color: '#fff' }

export default function PRReviewNode({ data, nodeStyle, icon }) {
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...DEFAULT_STYLE, ...nodeStyle }}
      icon={icon ?? <GitPullRequest className="w-4 h-4" />}
    />
  )
}
