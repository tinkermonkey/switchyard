import CycleBoundingNode from './CycleBoundingNode'
import ReviewCycleContainerNode from './ReviewCycleContainerNode'
import RepairCycleContainerNode from './RepairCycleContainerNode'
import PRReviewCycleContainerNode from './PRReviewCycleContainerNode'
import ConversationalLoopContainerNode from './ConversationalLoopContainerNode'
import IterationContainerNode from './IterationContainerNode'
import SubCycleContainerNode from './SubCycleContainerNode'

export const containerNodeTypes = {
  // Level 1 — top-level cycle bounding containers
  cycleBounding:               CycleBoundingNode,
  reviewCycleContainer:        ReviewCycleContainerNode,
  repairCycleContainer:        RepairCycleContainerNode,
  prReviewCycleContainer:      PRReviewCycleContainerNode,
  conversationalLoopContainer: ConversationalLoopContainerNode,

  // Level 2 — iteration/phase containers (child of cycle container)
  // Theme is driven by data.cycleType: 'review' | 'repair' | 'pr_review'
  iterationContainer:          IterationContainerNode,

  // Level 3 — sub-cycle containers (child of iterationContainer, repair cycles only)
  // Theme is driven by data.cycleType: 'test_execution' | 'fix_cycle' | 'warning_review' | 'systemic_analysis' | 'systemic_fix'
  subCycleContainer:           SubCycleContainerNode,
}
