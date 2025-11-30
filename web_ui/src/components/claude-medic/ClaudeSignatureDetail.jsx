import { useEffect, useState } from 'react'
import { useParams, Link } from '@tanstack/react-router'
import { ArrowLeft, Play, FileText, TrendingUp, Clock, AlertCircle, Code2, FolderGit2, Wrench, Loader2 } from 'lucide-react'
import Header from '../Header'
import NavigationTabs from '../NavigationTabs'
import ClaudeClusterView from './ClaudeClusterView'
import ClaudeRecommendations from './ClaudeRecommendations'
import ClaudeDiagnosis from './ClaudeDiagnosis'

export default function ClaudeSignatureDetail() {
  const { fingerprintId } = useParams({ from: '/claude-medic-detail/$fingerprintId' })
  const [signature, setSignature] = useState(null)
  const [clusters, setClusters] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showRecommendations, setShowRecommendations] = useState(false)
  const [showDiagnosis, setShowDiagnosis] = useState(false)
  const [investigationStatus, setInvestigationStatus] = useState(null)
  const [fixStatus, setFixStatus] = useState(null)

  useEffect(() => {
    if (fingerprintId) {
      fetchSignature()
      fetchClusters()
      fetchInvestigationStatus()
      fetchFixStatus()
    }
  }, [fingerprintId])

  // Poll investigation status if queued or in progress
  useEffect(() => {
    let interval
    if (investigationStatus && (investigationStatus.queue_status === 'queued' || investigationStatus.queue_status === 'starting' || investigationStatus.queue_status === 'in_progress')) {
      interval = setInterval(fetchInvestigationStatus, 2000)
    }
    return () => clearInterval(interval)
  }, [investigationStatus])

  // Poll fix status if in progress
  useEffect(() => {
    let interval
    if (fixStatus && (fixStatus.status === 'queued' || fixStatus.status === 'in_progress')) {
      interval = setInterval(fetchFixStatus, 2000)
    }
    return () => clearInterval(interval)
  }, [fixStatus])

  const fetchSignature = async () => {
    try {
      const response = await fetch(`/api/medic/claude/failure-signatures/${fingerprintId}`)
      if (!response.ok) throw new Error('Failed to fetch signature')
      const data = await response.json()
      setSignature(data)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const fetchInvestigationStatus = async () => {
    try {
      const response = await fetch(`/api/medic/claude/investigations/${fingerprintId}/status`)
      if (response.ok) {
        const data = await response.json()
        setInvestigationStatus(data)
      }
    } catch (err) {
      console.error('Error fetching investigation status:', err)
    }
  }

  const fetchFixStatus = async () => {
    try {
      const response = await fetch(`/api/medic/claude/fixes/${fingerprintId}/status`)
      if (response.ok) {
        const data = await response.json()
        setFixStatus(data)
      }
    } catch (err) {
      console.error('Error fetching fix status:', err)
    }
  }

  const fetchClusters = async () => {
    try {
      const response = await fetch(`/api/medic/claude/failure-signatures/${fingerprintId}/clusters`)
      if (!response.ok) throw new Error('Failed to fetch clusters')
      const data = await response.json()
      setClusters(data.clusters || [])
    } catch (err) {
      console.error('Error fetching clusters:', err)
    }
  }

  const triggerInvestigation = async () => {
    try {
      const response = await fetch(`/api/medic/claude/investigations/${fingerprintId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ priority: 'high' })
      })
      if (!response.ok) throw new Error('Failed to trigger investigation')
      fetchSignature() // Refresh to update investigation status
      fetchInvestigationStatus()
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  const triggerFix = async () => {
    try {
      const response = await fetch(`/api/medic/claude/fixes/${fingerprintId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      })
      if (!response.ok) throw new Error('Failed to trigger fix')
      fetchFixStatus()
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  if (loading) {
    return (
      <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
        <Header />
        <NavigationTabs />
        <div className="flex items-center justify-center p-8 mt-6">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
        </div>
      </div>
    )
  }

  if (error || !signature) {
    return (
      <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
        <Header />
        <NavigationTabs />
        <div className="mt-6">
          <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
            <p className="text-sm text-red-500">Error: {error || 'Signature not found'}</p>
          </div>
        </div>
      </div>
    )
  }

  const getStatusColor = (status) => {
    switch (status) {
      case 'new': return 'text-blue-500 bg-blue-500/10 border-blue-500/20'
      case 'recurring': return 'text-yellow-500 bg-yellow-500/10 border-yellow-500/20'
      case 'trending': return 'text-orange-500 bg-orange-500/10 border-orange-500/20'
      case 'resolved': return 'text-green-500 bg-green-500/10 border-green-500/20'
      case 'ignored': return 'text-gray-500 bg-gray-500/10 border-gray-500/20'
      default: return 'text-gray-500 bg-gray-500/10 border-gray-500/20'
    }
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />
      <NavigationTabs />

      <div className="mt-6 space-y-6">
        {/* Back Button */}
        <Link
          to="/claude-medic"
          className="inline-flex items-center gap-2 text-sm text-gh-accent-fg hover:underline"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to Claude Medic
        </Link>

        {/* Signature Header */}
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-6">
          <div className="flex items-start justify-between mb-4">
            <div className="flex-1">
              <h2 className="text-xl font-semibold text-gh-fg mb-2">
                Failure Signature
              </h2>
              <div className="flex items-center gap-2 mb-3">
                <span className={`px-2 py-1 rounded text-xs font-medium border ${getStatusColor(signature.status)}`}>
                  {signature.status}
                </span>
                <span className="px-2 py-1 rounded text-xs bg-gh-canvas border border-gh-border flex items-center gap-1">
                  <FolderGit2 className="w-3 h-3" />
                  {signature.project}
                </span>
                <span className="px-2 py-1 rounded text-xs bg-gh-canvas border border-gh-border flex items-center gap-1">
                  <Code2 className="w-3 h-3" />
                  {signature.signature?.tool_name}
                </span>
              </div>
              <p className="text-sm text-gh-fg-muted font-mono">
                {fingerprintId}
              </p>
            </div>

            {(signature.investigation_status === 'not_started' || signature.investigation_status === 'failed') && (
              <button
                onClick={triggerInvestigation}
                className="px-4 py-2 bg-gh-accent-emphasis text-white rounded hover:bg-gh-accent-primary transition-colors flex items-center gap-2"
              >
                <Play className="w-4 h-4" />
                {signature.investigation_status === 'failed' ? 'Retry Investigation' : 'Start Investigation'}
              </button>
            )}
          </div>

          {/* Error Pattern */}
          <div className="mb-4">
            <h3 className="text-sm font-semibold text-gh-fg mb-2">Error Pattern</h3>
            <p className="text-sm text-gh-fg">{signature.signature?.error_type}: {signature.signature?.error_pattern}</p>
            {signature.signature?.context_signature && (
              <p className="text-xs text-gh-fg-muted mt-1 font-mono">
                Context: {signature.signature.context_signature}
              </p>
            )}
          </div>

          {/* Statistics */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatItem
              icon={<TrendingUp className="w-4 h-4" />}
              label="Total Clusters"
              value={signature.cluster_count || 0}
            />
            <StatItem
              icon={<AlertCircle className="w-4 h-4" />}
              label="Total Failures"
              value={signature.total_failures || 0}
            />
            <StatItem
              icon={<TrendingUp className="w-4 h-4" />}
              label="Avg Cluster Size"
              value={(signature.signature?.cluster_size_avg || 0).toFixed(1)}
            />
            <StatItem
              icon={<Clock className="w-4 h-4" />}
              label="Last Seen"
              value={new Date(signature.last_seen).toLocaleString()}
              small
            />
          </div>
        </div>

        {/* Investigation Status */}
        {investigationStatus && investigationStatus.queue_status && investigationStatus.queue_status !== 'not_started' && (
          <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gh-fg mb-3 flex items-center gap-2">
              <FileText className="w-4 h-4" />
              Investigation Status: {investigationStatus.report_status || investigationStatus.queue_status}
              {(investigationStatus.queue_status === 'queued' || investigationStatus.queue_status === 'starting' || investigationStatus.queue_status === 'in_progress') && (
                <Loader2 className="w-4 h-4 ml-2 animate-spin text-blue-500" />
              )}
            </h3>

            {/* Show progress message for queued/in-progress investigations */}
            {(investigationStatus.queue_status === 'queued' || investigationStatus.queue_status === 'starting' || investigationStatus.queue_status === 'in_progress') && (
              <p className="text-sm text-gh-fg-muted">
                {investigationStatus.queue_status === 'queued' && 'Investigation queued. Waiting for investigator to start...'}
                {investigationStatus.queue_status === 'starting' && 'Investigation starting...'}
                {investigationStatus.queue_status === 'in_progress' && `Investigation in progress... (${investigationStatus.progress_lines || 0} log lines written)`}
              </p>
            )}

            {(investigationStatus.queue_status === 'completed' || investigationStatus.report_status === 'diagnosed') && (
              <div className="flex flex-wrap gap-3 items-center">
                <button
                  onClick={() => {
                    setShowDiagnosis(true)
                    setShowRecommendations(false)
                  }}
                  className={`px-4 py-2 rounded text-sm transition-colors ${
                    showDiagnosis
                      ? 'bg-gh-accent-emphasis text-white'
                      : 'bg-gh-canvas border border-gh-border hover:bg-gh-border-muted'
                  }`}
                >
                  View Diagnosis
                </button>
                <button
                  onClick={() => {
                    setShowRecommendations(true)
                    setShowDiagnosis(false)
                  }}
                  className={`px-4 py-2 rounded text-sm transition-colors ${
                    showRecommendations
                      ? 'bg-gh-accent-emphasis text-white'
                      : 'bg-gh-canvas border border-gh-border hover:bg-gh-border-muted'
                  }`}
                >
                  View Recommendations
                </button>

                {/* Fix Button */}
                {investigationStatus?.has_fix_plan && (
                  <div className="flex items-center gap-2 ml-auto">
                    {fixStatus && (fixStatus.status === 'queued' || fixStatus.status === 'in_progress') ? (
                      <div className="flex items-center gap-2 px-4 py-2 bg-blue-500/10 border border-blue-500/20 rounded text-blue-500 text-sm">
                        <Loader2 className="w-4 h-4 animate-spin" />
                        <span>Fixing... {fixStatus.status}</span>
                      </div>
                    ) : fixStatus && fixStatus.status === 'completed' ? (
                      <div className="flex items-center gap-2 px-4 py-2 bg-green-500/10 border border-green-500/20 rounded text-green-500 text-sm">
                        <Wrench className="w-4 h-4" />
                        <span>Fix Applied</span>
                      </div>
                    ) : (
                      <button
                        onClick={triggerFix}
                        className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 transition-colors flex items-center gap-2 text-sm font-medium"
                      >
                        <Wrench className="w-4 h-4" />
                        Apply Fix
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* Fix Status Details */}
            {fixStatus && fixStatus.status === 'failed' && (
              <div className="mt-3 p-3 bg-red-500/10 border border-red-500/20 rounded text-sm text-red-500">
                <p className="font-semibold">Fix Failed:</p>
                <p>{fixStatus.error || 'Unknown error'}</p>
              </div>
            )}
          </div>
        )}

        {/* Recommendations */}
        {showRecommendations && investigationStatus?.report_status === 'diagnosed' && (
          <ClaudeRecommendations fingerprintId={fingerprintId} />
        )}

        {/* Diagnosis */}
        {showDiagnosis && investigationStatus?.report_status === 'diagnosed' && (
          <ClaudeDiagnosis fingerprintId={fingerprintId} />
        )}

        {/* Clusters */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-lg font-semibold text-gh-fg">Clusters ({clusters.length})</h3>
          </div>

          {clusters.length === 0 ? (
            <div className="p-8 text-center bg-gh-canvas-subtle border border-gh-border rounded-lg">
              <p className="text-sm text-gh-fg-muted">No clusters available</p>
            </div>
          ) : (
            <div className="space-y-3">
              {clusters.map((cluster, idx) => (
                <ClaudeClusterView key={idx} cluster={cluster} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function StatItem({ icon, label, value, small = false }) {
  return (
    <div className="bg-gh-canvas border border-gh-border rounded p-3">
      <div className="flex items-center gap-2 mb-1 text-gh-fg-muted">
        {icon}
        <span className="text-xs">{label}</span>
      </div>
      <p className={`font-semibold text-gh-fg ${small ? 'text-xs' : 'text-lg'}`}>
        {value}
      </p>
    </div>
  )
}
