import { Activity, CheckCircle, XCircle } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const KNOWN_STATUSES = new Set(['running', 'completed', 'failed', undefined])

export default function AgentExecutionNode({ data }) {
  if (!data) return null
  const { status, isActive } = data

  if (process.env.NODE_ENV !== 'production' && status !== undefined && !KNOWN_STATUSES.has(status)) {
    console.warn(`AgentExecutionNode: unrecognised status "${status}" — add a style mapping or update KNOWN_STATUSES`)
  }

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
