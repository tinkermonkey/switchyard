import Header from './Header'
import NavigationTabs from './NavigationTabs'
import ProjectCard from './ProjectCard'
import { useProjects } from '../hooks/useProjects'

export default function Projects() {
  const { projects, loading, error } = useProjects()

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
      <Header />

      <NavigationTabs />

      <div className="mt-6">
        <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg px-6 py-4">
          <h2 className="text-xl font-semibold text-gh-fg">Configured Projects</h2>
          <p className="text-sm text-gh-fg-muted mt-1">
            {loading ? 'Loading...' : `${projects.length} project${projects.length !== 1 ? 's' : ''} configured`}
          </p>
        </div>

        {error && (
          <div className="px-6 py-4 bg-red-500/10 border-b border-red-500/20">
            <p className="text-sm text-red-500">Error: {error}</p>
          </div>
        )}

        {loading ? (
          <div className="px-6 py-8 text-center">
            <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-gh-accent-emphasis"></div>
            <p className="mt-2 text-sm text-gh-fg-muted">Loading projects...</p>
          </div>
        ) : (
          <div className="space-y-4 mt-4">
            {projects.map((project) => (
              <div className="bg-gh-canvas-subtle border border-gh-border rounded-lg">
                <ProjectCard key={project.name} project={project} />
              </div>
            ))}

            {projects.length === 0 && !loading && (
              <div className="px-6 py-8 text-center">
                <p className="text-sm text-gh-fg-muted">No projects configured</p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
