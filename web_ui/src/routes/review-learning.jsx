import { createFileRoute } from '@tanstack/react-router'
import ReviewLearning from '../components/ReviewLearning'

export const Route = createFileRoute('/review-learning')({
  component: ReviewLearning,
})
