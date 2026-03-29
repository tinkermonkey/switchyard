import CycleBoundingNode from './CycleBoundingNode'
import CycleContainerNode from './CycleContainerNode'

export const containerNodeTypes = {
  // Legacy bounding node (separate theme map, kept for compatibility)
  cycleBounding:               CycleBoundingNode,

  // Level 1 — top-level cycle containers (theme driven by data.cycleType)
  reviewCycleContainer:        CycleContainerNode,
  repairCycleContainer:        CycleContainerNode,
  prReviewCycleContainer:      CycleContainerNode,
  conversationalLoopContainer: CycleContainerNode,
  statusProgressionContainer:  CycleContainerNode,
  agentExecutionContainer:     CycleContainerNode,

  // Level 2 — iteration/phase containers (theme driven by data.cycleType)
  iterationContainer:          CycleContainerNode,

  // Level 3 — sub-cycle containers (theme driven by data.cycleType)
  subCycleContainer:           CycleContainerNode,
}
