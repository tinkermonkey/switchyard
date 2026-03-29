import { CircleCheck } from 'lucide-react'
import PipelineEventNode from '../PipelineEventNode'

const STYLE_COMPLETED = { background: '#0f1f0f', borderColor: '#2ea043', color: '#b0f0b0' }
const STYLE_FAILED    = { background: '#1e0a0a', borderColor: '#f85149', color: '#ffd2d2' }

export default function AgentLifecycleNode({ data, nodeStyle, icon }) {
  const isCompleted = data?.event?.event_type === 'agent_completed'
  const defaultStyle = isCompleted ? STYLE_COMPLETED : STYLE_FAILED
  return (
    <PipelineEventNode
      data={data}
      nodeStyle={{ ...defaultStyle, ...nodeStyle }}
      icon={icon}
    />
  )
}
