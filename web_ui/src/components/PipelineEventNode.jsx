import { Activity, CheckCircle, XCircle, AlertCircle, MessageSquare, GitBranch, PlayCircle } from 'lucide-react'
import { Handle, Position } from '@xyflow/react'

/**
 * Custom node component for pipeline run events with candy-stripe animation
 */
export default function PipelineEventNode({ data }) {
  const { label, type, status, metadata, isActive } = data

  const getNodeStyle = () => {
    const baseStyle = {
      padding: '12px 16px',
      borderRadius: '8px',
      border: '2px solid',
      minWidth: '200px',
      maxWidth: '300px',
      boxShadow: isActive ? '0 0 10px rgba(88, 166, 255, 0.5)' : '0 2px 4px rgba(0,0,0,0.1)',
    }

    switch (type) {
      case 'pipeline_created':
        return { ...baseStyle, background: '#10b981', borderColor: '#059669', color: '#fff' }
      case 'pipeline_completed':
        return { ...baseStyle, background: '#6366f1', borderColor: '#4f46e5', color: '#fff' }
      case 'decision_event': {
        const getDecisionColors = (category) => {
          switch (category) {
            case 'routing':          return { bg: '#3b82f6', border: '#2563eb' }
            case 'progression':      return { bg: '#10b981', border: '#059669' }
            case 'review_cycle':     return { bg: '#8b5cf6', border: '#7c3aed' }
            case 'feedback':         return { bg: '#f59e0b', border: '#d97706' }
            case 'error_handling':   return { bg: '#ef4444', border: '#dc2626' }
            case 'task_management':  return { bg: '#06b6d4', border: '#0891b2' }
            case 'branch_management':return { bg: '#84cc16', border: '#65a30d' }
            case 'conversational_loop': return { bg: '#ec4899', border: '#db2777' }
            default:                 return { bg: '#f59e0b', border: '#d97706' }
          }
        }
        const colors = getDecisionColors(data.decision_category)
        return { ...baseStyle, background: colors.bg, borderColor: colors.border, color: '#fff' }
      }
      case 'agent_execution':
        if (status === 'running' || isActive) {
          return { ...baseStyle, background: '#1f6feb', borderColor: '#58a6ff', color: '#fff', border: '3px solid #58a6ff' }
        } else if (status === 'completed') {
          return { ...baseStyle, background: '#238636', borderColor: '#2ea043', color: '#fff' }
        } else if (status === 'failed') {
          return { ...baseStyle, background: '#da3633', borderColor: '#f85149', color: '#fff' }
        }
        return { ...baseStyle, background: '#6e7681', borderColor: '#30363d', color: '#fff' }
      case 'review_feedback':
        return { ...baseStyle, background: '#8b5cf6', borderColor: '#7c3aed', color: '#fff' }
      case 'human_feedback':
        return { ...baseStyle, background: '#ec4899', borderColor: '#db2777', color: '#fff' }
      default:
        return { ...baseStyle, background: '#374151', borderColor: '#4b5563', color: '#fff' }
    }
  }

  const getIcon = () => {
    switch (type) {
      case 'pipeline_created':  return <PlayCircle className="w-4 h-4" />
      case 'pipeline_completed':return <CheckCircle className="w-4 h-4" />
      case 'decision_event':    return <GitBranch className="w-4 h-4" />
      case 'agent_execution':
        if (status === 'completed') return <CheckCircle className="w-4 h-4" />
        if (status === 'failed')    return <XCircle className="w-4 h-4" />
        return <Activity className="w-4 h-4" />
      case 'review_feedback':   return <MessageSquare className="w-4 h-4" />
      case 'human_feedback':    return <AlertCircle className="w-4 h-4" />
      default:                  return <Activity className="w-4 h-4" />
    }
  }

  return (
    <div style={getNodeStyle()} className="relative">
      {/* Candy stripe animation for active agents */}
      {isActive && (
        <div
          className="absolute top-0 left-0 right-0 h-1 rounded-t-md overflow-hidden"
          style={{
            backgroundImage: 'linear-gradient(45deg, rgba(255,255,255,.2) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.2) 50%, rgba(255,255,255,.2) 75%, transparent 75%, transparent)',
            backgroundSize: '1rem 1rem',
            animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite, stripes 1s linear infinite'
          }}
        />
      )}

      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />

      <div className="flex items-start gap-2">
        <div className="mt-0.5">{getIcon()}</div>
        <div className="flex-1">
          <div className="font-semibold text-sm">{label}</div>
          {metadata && (
            <div className="text-xs mt-1 opacity-90">{metadata}</div>
          )}
        </div>
      </div>

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}
