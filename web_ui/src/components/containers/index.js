import CycleBoundingNode from './CycleBoundingNode'
import CycleContainerNode from './CycleContainerNode'

export const containerNodeTypes = {
  // Legacy bounding node (separate theme map, kept for compatibility)
  cycleBounding:               CycleBoundingNode,

  // Universal cycle/iteration container (theme driven by data.cycleType)
  subCycleContainer:           CycleContainerNode,

  // Iteration/phase containers within cycle containers
  iterationContainer:          CycleContainerNode,
}
