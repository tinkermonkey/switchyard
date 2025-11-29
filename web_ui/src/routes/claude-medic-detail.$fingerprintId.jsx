import { createFileRoute } from '@tanstack/react-router'
import ClaudeSignatureDetail from '../components/claude-medic/ClaudeSignatureDetail'

export const Route = createFileRoute('/claude-medic-detail/$fingerprintId')({
  component: ClaudeSignatureDetail,
})
