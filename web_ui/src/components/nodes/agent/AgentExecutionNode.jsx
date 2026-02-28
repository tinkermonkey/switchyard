import { Activity, CheckCircle, XCircle } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

export default function AgentExecutionNode({ data }) {
  const { status, isActive } = data
  let nodeStyle, icon
  if (isActive || status === 'running') {
    nodeStyle = { background: '#1f6feb', borderColor: '#58a6ff', color: '#fff', border: '3px solid #58a6ff' }
    icon = <Activity className="w-4 h-4" />
  } else if (status === 'completed') {
    nodeStyle = { background: '#238636', borderColor: '#2ea043', color: '#fff' }
    icon = <CheckCircle className="w-4 h-4" />
  } else if (status === 'failed') {
    nodeStyle = { background: '#da3633', borderColor: '#f85149', color: '#fff' }
    icon = <XCircle className="w-4 h-4" />
  } else {
    nodeStyle = { background: '#6e7681', borderColor: '#30363d', color: '#fff' }
    icon = <Activity className="w-4 h-4" />
  }
  return <PipelineEventNode data={data} nodeStyle={nodeStyle} icon={icon} />
}
