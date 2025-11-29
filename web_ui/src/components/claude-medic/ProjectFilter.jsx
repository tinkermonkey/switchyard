import { useEffect, useState } from 'react'
import { FolderGit2 } from 'lucide-react'

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
    <div className="flex items-center gap-2">
      <FolderGit2 className="w-4 h-4 text-gh-fg-muted" />
      <label className="text-sm text-gh-fg-muted">Project:</label>
      <select
        value={selectedProject}
        onChange={(e) => onProjectChange(e.target.value)}
        className="px-3 py-1.5 bg-gh-canvas border border-gh-border rounded text-sm min-w-[200px]"
        disabled={loading}
      >
        <option value="">All Projects</option>
        {projects.map((project) => (
          <option key={project.name} value={project.name}>
            {project.name} ({project.failure_count})
          </option>
        ))}
      </select>
    </div>
  )
}
