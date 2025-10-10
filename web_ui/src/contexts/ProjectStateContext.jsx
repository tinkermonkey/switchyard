import { createContext, useContext, useEffect, useState } from 'react'
import { projectApi } from '../services/projectApi'
import { startPolling, POLLING_INTERVALS } from '../utils/polling'

const ProjectStateContext = createContext()

/**
 * ProjectStateProvider - Manages project data and configurations
 * Polls project endpoints and caches data
 */
export function ProjectStateProvider({ children }) {
  const [projects, setProjects] = useState([])
  const [projectsById, setProjectsById] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Fetch all projects
  const fetchProjects = async () => {
    try {
      const data = await projectApi.getProjects()

      if (data.success) {
        const projectList = data.projects || []
        setProjects(projectList)

        // Build indexed lookup
        const indexed = {}
        projectList.forEach((project) => {
          indexed[project.name] = project
        })
        setProjectsById(indexed)
        setError(null)
      } else {
        setError(data.error || 'Failed to fetch projects')
      }
    } catch (err) {
      console.error('Error fetching projects:', err)
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  // Set up polling for projects
  useEffect(() => {
    const cleanup = startPolling(fetchProjects, POLLING_INTERVALS.PROJECTS)
    return cleanup
  }, [])

  // Helper to get a specific project
  const getProject = (projectName) => {
    return projectsById[projectName] || null
  }

  const value = {
    // Project state
    projects,
    projectsById,
    loading,
    error,

    // Helpers
    getProject,
    projectCount: projects.length,

    // Manual refresh
    refreshProjects: fetchProjects,
  }

  return (
    <ProjectStateContext.Provider value={value}>
      {children}
    </ProjectStateContext.Provider>
  )
}

/**
 * Hook to access project state
 */
export function useProjectState() {
  const context = useContext(ProjectStateContext)
  if (!context) {
    throw new Error('useProjectState must be used within ProjectStateProvider')
  }
  return context
}
