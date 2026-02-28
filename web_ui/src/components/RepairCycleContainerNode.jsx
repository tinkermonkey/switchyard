import { memo } from 'react'
import { Wrench } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

const REPAIR_CYCLE_THEME = {
  borderColor:        '#d97706',
  bgColor:            'rgba(217, 119, 6, 0.14)',
  cornerColor:        'rgba(217,119,6,0.6)',
  icon:               Wrench,
  countSuffix:        'test cycle',
  collapsedLabel:     'Repair Cycle',
  collapsedTextColor: '#fcd34d',
  collapsedCountColor:'#fef3c7',
}

const RepairCycleContainerNode = props => <CycleContainerNode {...props} theme={REPAIR_CYCLE_THEME} />

export default memo(RepairCycleContainerNode)
