import { HelpCircle } from 'lucide-react'
import ConversationalLoopNode from './ConversationalLoopNode'

export default function ConversationalQuestionRoutedNode({ data }) {
  return <ConversationalLoopNode data={data} icon={<HelpCircle className="w-4 h-4" />} />
}
