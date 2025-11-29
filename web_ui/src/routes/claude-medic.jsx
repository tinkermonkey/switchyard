import { createFileRoute } from '@tanstack/react-router'
import ClaudeMedic from '../components/ClaudeMedic'

export const Route = createFileRoute('/claude-medic')({
  component: ClaudeMedic,
})
