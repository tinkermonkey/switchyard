/**
 * HeaderActiveAgents - Wraps ActiveAgents in a header box
 */
import ActiveAgents from './ActiveAgents'
import HeaderBox from './HeaderBox'

export default function HeaderActiveAgents() {
  return <ActiveAgents ContainerComponent={HeaderBox} />
}
