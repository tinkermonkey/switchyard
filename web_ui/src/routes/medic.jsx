import { createFileRoute } from '@tanstack/react-router'
import Medic from '../components/Medic'

export const Route = createFileRoute('/medic')({
  component: Medic,
})
