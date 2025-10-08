import DevContainerStatus from './DevContainerStatus'
import WorkspaceStatus from './WorkspaceStatus'
import PipelinesStatus from './PipelinesStatus'

export default function ProjectCard({ project }) {
  return (
    <div className="px-6 py-4 hover:bg-gh-canvas-inset transition-colors">
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h3 className="text-lg font-semibold text-gh-fg">{project.name}</h3>
            {project.github?.url && (
              <a
                href={project.github.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-gh-accent-fg hover:underline"
              >
                {project.github.org}/{project.github.repo}
              </a>
            )}
          </div>

          <div className="mt-3 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <DevContainerStatus devContainer={project.dev_container} />
            <WorkspaceStatus workspace={project.workspace} />
            <PipelinesStatus pipelines={project.pipelines} />
          </div>
        </div>
      </div>
    </div>
  )
}
