import { AlertCircle } from 'lucide-react'
import ErrorEventNode from './ErrorEventNode'

export default function ErrorEncounteredNode({ data }) {
  return <ErrorEventNode data={data} icon={<AlertCircle className="w-4 h-4" />} />
}
