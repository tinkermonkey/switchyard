import { useState, useEffect } from 'react'
import { Plus, Trash2, Pencil, CheckCircle, XCircle } from 'lucide-react'
import Header from './Header'
import NavigationTabs from './NavigationTabs'

const API_BASE = 'http://localhost:5001'

export default function ReviewLearning() {
  const [filters, setFilters] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedAgent, setSelectedAgent] = useState('all')
  const [showForm, setShowForm] = useState(false)
  const [editingFilter, setEditingFilter] = useState(null)
  const [agents, setAgents] = useState([])

  // Fetch available agents
  useEffect(() => {
    fetch(`${API_BASE}/api/review-filters/agents`)
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          setAgents(data.agents)
        }
      })
      .catch(err => console.error('Error fetching agents:', err))
  }, [])

  // Fetch filters
  const fetchFilters = () => {
    setLoading(true)
    const url = selectedAgent === 'all'
      ? `${API_BASE}/api/review-filters`
      : `${API_BASE}/api/review-filters?agent=${selectedAgent}`

    fetch(url)
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          setFilters(data.filters)
        } else {
          setError(data.error)
        }
        setLoading(false)
      })
      .catch(err => {
        setError(err.message)
        setLoading(false)
      })
  }

  useEffect(() => {
    fetchFilters()
  }, [selectedAgent])

  const toggleFilter = async (filterId) => {
    try {
      const res = await fetch(`${API_BASE}/api/review-filters/${filterId}/toggle`, {
        method: 'POST'
      })
      const data = await res.json()
      if (data.success) {
        fetchFilters()
      }
    } catch (err) {
      console.error('Error toggling filter:', err)
    }
  }

  const deleteFilter = async (filterId) => {
    if (!confirm('Are you sure you want to delete this filter?')) return

    try {
      const res = await fetch(`${API_BASE}/api/review-filters/${filterId}`, {
        method: 'DELETE'
      })
      const data = await res.json()
      if (data.success) {
        fetchFilters()
      }
    } catch (err) {
      console.error('Error deleting filter:', err)
    }
  }

  const handleEdit = (filter) => {
    setEditingFilter(filter)
    setShowForm(true)
  }

  const handleFormClose = () => {
    setShowForm(false)
    setEditingFilter(null)
    fetchFilters()
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />

      {/* Navigation */}
      <NavigationTabs />

      {/* Page Header */}
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gh-fg mb-2">Review Learning</h1>
        <p className="text-gh-muted">Manage review filters to reduce noise and highlight important patterns</p>
      </div>

      {/* Toolbar */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-4">
          <label className="text-gh-fg text-sm">Filter by agent:</label>
          <select
            value={selectedAgent}
            onChange={(e) => setSelectedAgent(e.target.value)}
            className="bg-gh-canvas-subtle border border-gh-border text-gh-fg rounded px-3 py-2 text-sm"
          >
            <option value="all">All Agents</option>
            {agents.map(agent => (
              <option key={agent} value={agent}>
                {agent.replace('_', ' ').split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}
              </option>
            ))}
          </select>
        </div>

        <button
          onClick={() => setShowForm(true)}
          className="flex items-center space-x-2 bg-gh-success text-white px-4 py-2 rounded hover:bg-gh-success-emphasis transition-colors"
        >
          <Plus className="w-5 h-5" />
          <span>New Filter</span>
        </button>
      </div>

      {/* Filter List */}
      {loading ? (
        <div className="text-center text-gh-muted py-12">Loading filters...</div>
      ) : error ? (
        <div className="bg-gh-danger text-white p-4 rounded mb-4">{error}</div>
      ) : filters.length === 0 ? (
        <div className="text-center text-gh-muted py-12">
          <p>No filters found for {selectedAgent === 'all' ? 'any agent' : selectedAgent}</p>
          <button
            onClick={() => setShowForm(true)}
            className="mt-4 text-gh-accent hover:underline"
          >
            Create your first filter
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          {filters.map(filter => (
            <FilterCard
              key={filter.filter_id}
              filter={filter}
              onToggle={() => toggleFilter(filter.filter_id)}
              onEdit={() => handleEdit(filter)}
              onDelete={() => deleteFilter(filter.filter_id)}
            />
          ))}
        </div>
      )}

      {/* Filter Form Modal */}
      {showForm && (
        <FilterForm
          filter={editingFilter}
          agents={agents}
          onClose={handleFormClose}
        />
      )}
    </div>
  )
}

function FilterCard({ filter, onToggle, onEdit, onDelete }) {
  const actionColors = {
    highlight: 'bg-blue-100 text-blue-800 border-blue-300',
    suppress: 'bg-gray-100 text-gray-800 border-gray-300',
    adjust_severity: 'bg-yellow-100 text-yellow-800 border-yellow-300'
  }

  const severityColors = {
    critical: 'bg-red-100 text-red-800',
    high: 'bg-orange-100 text-orange-800',
    medium: 'bg-yellow-100 text-yellow-800',
    low: 'bg-blue-100 text-blue-800'
  }

  return (
    <div className={`bg-gh-canvas-subtle border border-gh-border rounded-lg p-6 ${!filter.active ? 'opacity-60' : ''}`}>
      <div className="flex justify-between items-start mb-4">
        <div className="flex-1">
          <div className="flex items-center space-x-3 mb-2">
            <h3 className="text-lg font-semibold text-gh-fg">{filter.pattern_description}</h3>
            <span className={`px-2 py-1 rounded text-xs border ${actionColors[filter.action]}`}>
              {filter.action.replace('_', ' ').toUpperCase()}
            </span>
            <span className={`px-2 py-1 rounded text-xs ${severityColors[filter.severity]}`}>
              {filter.severity.toUpperCase()}
            </span>
          </div>

          <div className="text-sm text-gh-muted mb-2">
            <span className="font-medium">Agent:</span> {filter.agent.replace('_', ' ')} |
            <span className="font-medium ml-2">Category:</span> {filter.category} |
            <span className="font-medium ml-2">Confidence:</span> {(filter.confidence * 100).toFixed(0)}%
          </div>

          <p className="text-gh-fg-muted text-sm mb-2">{filter.reason_ignored}</p>

          {filter.sample_findings && filter.sample_findings.length > 0 && (
            <details className="text-sm text-gh-muted mt-2">
              <summary className="cursor-pointer hover:text-gh-fg">Example findings</summary>
              <ul className="list-disc list-inside mt-2 space-y-1">
                {filter.sample_findings.map((sample, idx) => (
                  <li key={idx} className="text-xs">{sample}</li>
                ))}
              </ul>
            </details>
          )}
        </div>

        <div className="flex items-center space-x-2 ml-4">
          <button
            onClick={onToggle}
            className={`p-2 rounded hover:bg-gh-canvas-inset transition-colors ${filter.active ? 'text-gh-success' : 'text-gh-muted'}`}
            title={filter.active ? 'Deactivate' : 'Activate'}
          >
            {filter.active ? <CheckCircle className="w-5 h-5" /> : <XCircle className="w-5 h-5" />}
          </button>
          <button
            onClick={onEdit}
            className="p-2 rounded hover:bg-gh-canvas-inset text-gh-accent transition-colors"
            title="Edit"
          >
            <Pencil className="w-5 h-5" />
          </button>
          <button
            onClick={onDelete}
            className="p-2 rounded hover:bg-gh-canvas-inset text-gh-danger transition-colors"
            title="Delete"
          >
            <Trash2 className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  )
}

function FilterForm({ filter, agents, onClose }) {
  const [formData, setFormData] = useState({
    agent: filter?.agent || '',
    category: filter?.category || '',
    severity: filter?.severity || 'medium',
    pattern_description: filter?.pattern_description || '',
    reason_ignored: filter?.reason_ignored || '',
    sample_findings: filter?.sample_findings?.join('\n') || '',
    action: filter?.action || 'highlight',
    confidence: filter?.confidence || 0.90,
    from_severity: filter?.from_severity || '',
    to_severity: filter?.to_severity || ''
  })

  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)

    const data = {
      ...formData,
      sample_findings: formData.sample_findings.split('\n').filter(s => s.trim()),
      confidence: parseFloat(formData.confidence)
    }

    try {
      const url = filter
        ? `${API_BASE}/api/review-filters/${filter.filter_id}`
        : `${API_BASE}/api/review-filters`

      const method = filter ? 'PUT' : 'POST'

      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      })

      const result = await res.json()

      if (result.success) {
        onClose()
      } else {
        alert(`Error: ${result.error}`)
      }
    } catch (err) {
      alert(`Error: ${err.message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-gh-canvas-subtle rounded-lg p-8 max-w-2xl w-full max-h-[90vh] overflow-y-auto">
        <h2 className="text-2xl font-bold text-gh-fg mb-6">
          {filter ? 'Edit Filter' : 'Create New Filter'}
        </h2>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gh-fg mb-1">Agent</label>
            <select
              value={formData.agent}
              onChange={(e) => setFormData({ ...formData, agent: e.target.value })}
              required
              className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
            >
              <option value="">Select agent...</option>
              {agents.map(agent => (
                <option key={agent} value={agent}>
                  {agent.replace('_', ' ').split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}
                </option>
              ))}
            </select>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gh-fg mb-1">Category</label>
              <input
                type="text"
                value={formData.category}
                onChange={(e) => setFormData({ ...formData, category: e.target.value })}
                required
                placeholder="e.g., project_conventions, code_style"
                className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gh-fg mb-1">Severity</label>
              <select
                value={formData.severity}
                onChange={(e) => setFormData({ ...formData, severity: e.target.value })}
                required
                className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
              >
                <option value="critical">Critical</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gh-fg mb-1">Pattern Description</label>
            <textarea
              value={formData.pattern_description}
              onChange={(e) => setFormData({ ...formData, pattern_description: e.target.value })}
              required
              rows={2}
              placeholder="Describe the pattern this filter catches..."
              className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gh-fg mb-1">Reason</label>
            <textarea
              value={formData.reason_ignored}
              onChange={(e) => setFormData({ ...formData, reason_ignored: e.target.value })}
              rows={2}
              placeholder="Why is this filter important?"
              className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gh-fg mb-1">Sample Findings (one per line)</label>
            <textarea
              value={formData.sample_findings}
              onChange={(e) => setFormData({ ...formData, sample_findings: e.target.value })}
              rows={3}
              placeholder="Example findings that match this pattern..."
              className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gh-fg mb-1">Action</label>
              <select
                value={formData.action}
                onChange={(e) => setFormData({ ...formData, action: e.target.value })}
                required
                className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
              >
                <option value="highlight">Highlight (emphasize)</option>
                <option value="suppress">Suppress (ignore)</option>
                <option value="adjust_severity">Adjust Severity</option>
              </select>
            </div>

            <div>
              <label className="block text-sm font-medium text-gh-fg mb-1">Confidence</label>
              <input
                type="number"
                step="0.05"
                min="0"
                max="1"
                value={formData.confidence}
                onChange={(e) => setFormData({ ...formData, confidence: e.target.value })}
                required
                className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
              />
            </div>
          </div>

          {formData.action === 'adjust_severity' && (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gh-fg mb-1">From Severity</label>
                <select
                  value={formData.from_severity}
                  onChange={(e) => setFormData({ ...formData, from_severity: e.target.value })}
                  required
                  className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
                >
                  <option value="">Select...</option>
                  <option value="critical">Critical</option>
                  <option value="high">High</option>
                  <option value="medium">Medium</option>
                  <option value="low">Low</option>
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-gh-fg mb-1">To Severity</label>
                <select
                  value={formData.to_severity}
                  onChange={(e) => setFormData({ ...formData, to_severity: e.target.value })}
                  required
                  className="w-full bg-gh-canvas border border-gh-border text-gh-fg rounded px-3 py-2"
                >
                  <option value="">Select...</option>
                  <option value="critical">Critical</option>
                  <option value="high">High</option>
                  <option value="medium">Medium</option>
                  <option value="low">Low</option>
                </select>
              </div>
            </div>
          )}

          <div className="flex justify-end space-x-3 pt-4">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border text-gh-fg rounded hover:bg-gh-canvas-inset transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="px-4 py-2 bg-gh-success text-white rounded hover:bg-gh-success-emphasis transition-colors disabled:opacity-50"
            >
              {saving ? 'Saving...' : (filter ? 'Update Filter' : 'Create Filter')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
