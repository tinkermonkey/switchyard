import { ShieldOff } from 'lucide-react'
import ErrorEventNode from './ErrorEventNode'

export default function CircuitBreakerOpenedNode({ data }) {
  return <ErrorEventNode data={data} icon={<ShieldOff className="w-4 h-4" />} />
}
