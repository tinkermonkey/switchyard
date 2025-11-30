import { useEffect, useState } from 'react'

export default function ProjectFilter({ selectedProject, onProjectChange }) {
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchProjects()
  }, [])

  const fetchProjects = async () => {
    try {
      const response = await fetch('/api/medic/claude/projects')
      if (!response.ok) throw new Error('Failed to fetch projects')
      const data = await response.json()
      setProjects(data.projects || [])
    } catch (err) {
      console.error('Error fetching projects:', err)
    } finally {
      setLoading(false)
    }
  }

  return (
        <div className="w-48">
      <label className="block text-xs text-gh-fg-muted mb-1">Project</label>
      <select
        value={selectedProject || ''}
        onChange={(e) => onProjectChange(e.target.value || null)}
        className="w-full h-8 text-sm bg-gh-bg-secondary border border-gh-border rounded px-2 text-gh-fg-default focus:border-gh-blue focus:ring-1 focus:ring-gh-blue"
      >
        <option value="">All Projects</option>
        {projects.map((project) => (
          <option key={project.project} value={project.project}>
            {project.project} ({project.total_failures})
          </option>
        ))}
      </select>
    </div>
  )
}
