import { memo } from 'react'
import { GitPullRequest } from 'lucide-react'
import { CycleContainerNode } from './CycleContainerNode'

const PR_REVIEW_THEME = {
  borderColor:         '#0ea5e9',
  bgColor:             'rgba(14,165,233,0.12)',
  cornerColor:         'rgba(14,165,233,0.5)',
  icon:                GitPullRequest,
  countSuffix:         'phase',
  collapsedLabel:      'PR Review',
  collapsedTextColor:  '#7dd3fc',
  collapsedCountColor: '#e0f2fe',
}

const PRReviewCycleContainerNode = props => <CycleContainerNode {...props} theme={PR_REVIEW_THEME} />

export default memo(PRReviewCycleContainerNode)
