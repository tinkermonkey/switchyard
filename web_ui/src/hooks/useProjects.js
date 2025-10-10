/**
 * Selector hook for project data
 */
import { useProjectState } from '../contexts/ProjectStateContext'

export function useProjects() {
  const {
    projects,
    projectsById,
    loading,
    error,
    getProject,
    projectCount,
    refreshProjects,
  } = useProjectState()

  return {
    // Project data
    projects,
    projectsById,

    // Loading and error states
    loading,
    error,

    // Helpers
    getProject,
    projectCount,
    hasProjects: projectCount > 0,

    // Actions
    refresh: refreshProjects,
  }
}
