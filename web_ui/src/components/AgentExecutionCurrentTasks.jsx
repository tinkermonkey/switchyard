import { CheckCircle2, Circle, PlayCircle } from 'lucide-react'
import { formatTimestamp } from '../utils/eventMerging'

const getTodoStats = (todos) => {
  if (!todos || todos.length === 0) return { total: 0, completed: 0, inProgress: 0, pending: 0 }
  const completed = todos.filter(t => t.status === 'completed').length
  const inProgress = todos.filter(t => t.status === 'in_progress').length
  const pending = todos.filter(t => t.status === 'pending').length
  return { total: todos.length, completed, inProgress, pending }
}

export default function AgentExecutionCurrentTasks({ lastTodoWrite }) {
  const todoStats = getTodoStats(lastTodoWrite?.todos)

  return (
    <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
      <div className="mb-3">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-gh-fg">Current Tasks</h3>
            {lastTodoWrite && (
              <span className="text-xs text-gh-fg-muted">
                {formatTimestamp(lastTodoWrite.timestamp)}
              </span>
            )}
          </div>
          {lastTodoWrite && lastTodoWrite.todos.length > 0 && (
            <span className="text-xs text-gh-success">
              {todoStats.completed}/{todoStats.total}
            </span>
          )}
        </div>
      </div>
      {lastTodoWrite && lastTodoWrite.todos.length > 0 ? (
        <div className="space-y-2">
          {lastTodoWrite.todos.map((todo, idx) => {
            const isCompleted = todo.status === 'completed'
            const isInProgress = todo.status === 'in_progress'
            return (
              <div
                key={idx}
                className={`flex items-start gap-2 p-2 rounded ${
                  isInProgress ? 'bg-gh-warning-subtle border border-gh-warning' : ''
                }`}
              >
                {isCompleted ? (
                  <CheckCircle2 className="w-4 h-4 mt-0.5 text-gh-success flex-shrink-0" />
                ) : isInProgress ? (
                  <PlayCircle className="w-4 h-4 mt-0.5 text-gh-warning flex-shrink-0" />
                ) : (
                  <Circle className="w-4 h-4 mt-0.5 text-gh-fg-muted flex-shrink-0" />
                )}
                <span
                  className={`text-sm ${
                    isCompleted
                      ? 'line-through text-gh-fg-muted'
                      : isInProgress
                      ? 'text-gh-fg font-medium'
                      : 'text-gh-fg'
                  }`}
                >
                  {todo.content}
                </span>
              </div>
            )
          })}
        </div>
      ) : (
        <div className="text-center text-gh-fg-muted text-sm py-4">
          No current task list
        </div>
      )}
    </div>
  )
}
