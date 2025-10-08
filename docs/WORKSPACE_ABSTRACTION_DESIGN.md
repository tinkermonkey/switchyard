# Workspace Abstraction Layer Design

## Problem Statement

The orchestrator currently has 100+ conditional checks scattered across the codebase checking `workspace_type` ('issues', 'discussions', 'hybrid'). This creates:

- **High complexity**: Logic branches everywhere
- **Poor maintainability**: Changes require updating multiple files
- **Bug-prone**: Easy to miss a check or handle a case incorrectly
- **Poor extensibility**: Adding new workspace types requires widespread changes

## Current Pain Points

### 1. Feature Branch Management
```python
# agent_executor.py
if 'issue_number' in task_context and workspace_type != 'discussions':
    await feature_branch_manager.prepare_feature_branch(...)

if branch_name and workspace_type != 'discussions':
    await feature_branch_manager.finalize_feature_branch_work(...)
```

### 2. GitHub Posting
```python
# github_integration.py
if discussion_id or workspace_type in ['discussions', 'hybrid']:
    await self.discussions.add_comment(...)
else:
    await self.post_comment(issue_number, ...)
```

### 3. State Management
```python
# review_cycle.py
if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
    # discussions logic
elif cycle_state.workspace_type == 'issues':
    # issues logic
```

## Proposed Solution: Strategy Pattern

Create an abstraction layer using the **Strategy Pattern** where different workspace types are handled by polymorphic classes.

### Architecture Overview

```
┌─────────────────────────────────────────┐
│         AgentExecutor                   │
│  (orchestration logic, no conditionals) │
└────────────────┬────────────────────────┘
                 │
                 │ uses
                 ↓
┌─────────────────────────────────────────┐
│      WorkspaceContext (abstract)        │
│  + prepare_execution()                  │
│  + finalize_execution()                 │
│  + post_output()                        │
│  + get_working_directory()              │
│  + supports_git_operations()            │
└────────────────┬────────────────────────┘
                 │
         ┌───────┴────────┐
         │                │
┌────────▼──────┐  ┌──────▼──────────┐
│IssuesWorkspace│  │DiscussionsWork- │
│   Context     │  │  spaceContext   │
├───────────────┤  ├─────────────────┤
│+ prepare...() │  │+ prepare...()   │
│+ finalize...()│  │+ finalize...()  │
│+ post...()    │  │+ post...()      │
│+ supports_git │  │+ supports_git   │
│  = True       │  │  = False        │
└───────────────┘  └─────────────────┘
```

### Core Abstraction

```python
# services/workspace/context.py

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from pathlib import Path

class WorkspaceContext(ABC):
    """
    Abstract base class for workspace-specific execution contexts.

    Each workspace type (issues, discussions, hybrid) implements this
    interface to provide workspace-specific behavior without conditionals
    in the orchestration layer.
    """

    def __init__(
        self,
        project: str,
        issue_number: int,
        task_context: Dict[str, Any],
        github_integration
    ):
        self.project = project
        self.issue_number = issue_number
        self.task_context = task_context
        self.github = github_integration

    @property
    @abstractmethod
    def supports_git_operations(self) -> bool:
        """Whether this workspace supports git branch operations"""
        pass

    @property
    @abstractmethod
    def workspace_type(self) -> str:
        """The type of workspace ('issues', 'discussions', 'hybrid')"""
        pass

    @abstractmethod
    async def prepare_execution(self) -> Dict[str, Any]:
        """
        Prepare workspace for agent execution.

        Returns: Dict with workspace-specific context (e.g., branch_name, discussion_id)
        """
        pass

    @abstractmethod
    async def finalize_execution(
        self,
        result: Dict[str, Any],
        commit_message: str
    ) -> Dict[str, Any]:
        """
        Finalize workspace after agent execution.

        Returns: Dict with finalization results (e.g., pr_url, comment_id)
        """
        pass

    @abstractmethod
    async def post_output(
        self,
        agent_name: str,
        markdown_output: str
    ) -> Dict[str, Any]:
        """
        Post agent output to the appropriate location.

        Returns: Dict with posting results
        """
        pass

    @abstractmethod
    def get_working_directory(self) -> Path:
        """Get the working directory for this workspace"""
        pass

    @abstractmethod
    async def get_execution_metadata(self) -> Dict[str, Any]:
        """Get workspace-specific metadata for logging/observability"""
        pass


class WorkspaceContextFactory:
    """Factory for creating workspace contexts"""

    @staticmethod
    def create(
        workspace_type: str,
        project: str,
        issue_number: int,
        task_context: Dict[str, Any],
        github_integration
    ) -> WorkspaceContext:
        """Create appropriate workspace context based on type"""

        if workspace_type == 'issues':
            from .issues_context import IssuesWorkspaceContext
            return IssuesWorkspaceContext(
                project, issue_number, task_context, github_integration
            )
        elif workspace_type == 'discussions':
            from .discussions_context import DiscussionsWorkspaceContext
            return DiscussionsWorkspaceContext(
                project, issue_number, task_context, github_integration
            )
        elif workspace_type == 'hybrid':
            from .hybrid_context import HybridWorkspaceContext
            return HybridWorkspaceContext(
                project, issue_number, task_context, github_integration
            )
        else:
            raise ValueError(f"Unknown workspace type: {workspace_type}")
```

### Issues Workspace Implementation

```python
# services/workspace/issues_context.py

from pathlib import Path
from typing import Dict, Any
from .context import WorkspaceContext
from services.feature_branch_manager import feature_branch_manager

class IssuesWorkspaceContext(WorkspaceContext):
    """Workspace context for GitHub Issues with git operations"""

    def __init__(self, project, issue_number, task_context, github_integration):
        super().__init__(project, issue_number, task_context, github_integration)
        self.branch_name = None

    @property
    def supports_git_operations(self) -> bool:
        return True

    @property
    def workspace_type(self) -> str:
        return 'issues'

    async def prepare_execution(self) -> Dict[str, Any]:
        """Prepare feature branch and checkout"""

        issue_title = self.task_context.get('issue_title', '')

        self.branch_name = await feature_branch_manager.prepare_feature_branch(
            project=self.project,
            issue_number=self.issue_number,
            github_integration=self.github,
            issue_title=issue_title
        )

        return {
            'branch_name': self.branch_name,
            'work_dir': self.get_working_directory()
        }

    async def finalize_execution(
        self,
        result: Dict[str, Any],
        commit_message: str
    ) -> Dict[str, Any]:
        """Commit changes and create/update PR"""

        return await feature_branch_manager.finalize_feature_branch_work(
            project=self.project,
            issue_number=self.issue_number,
            commit_message=commit_message,
            github_integration=self.github
        )

    async def post_output(
        self,
        agent_name: str,
        markdown_output: str
    ) -> Dict[str, Any]:
        """Post output as issue comment"""

        await self.github.post_comment(
            self.issue_number,
            markdown_output
        )

        return {
            'success': True,
            'posted_to': f'issue #{self.issue_number}'
        }

    def get_working_directory(self) -> Path:
        """Get project git repository directory"""
        from services.project_workspace import workspace_manager
        return workspace_manager.get_project_dir(self.project)

    async def get_execution_metadata(self) -> Dict[str, Any]:
        """Get metadata for observability"""
        return {
            'workspace_type': 'issues',
            'issue_number': self.issue_number,
            'branch_name': self.branch_name
        }
```

### Discussions Workspace Implementation

```python
# services/workspace/discussions_context.py

from pathlib import Path
from typing import Dict, Any
from .context import WorkspaceContext

class DiscussionsWorkspaceContext(WorkspaceContext):
    """Workspace context for GitHub Discussions (no git operations)"""

    def __init__(self, project, issue_number, task_context, github_integration):
        super().__init__(project, issue_number, task_context, github_integration)
        self.discussion_id = task_context.get('discussion_id')

    @property
    def supports_git_operations(self) -> bool:
        return False

    @property
    def workspace_type(self) -> str:
        return 'discussions'

    async def prepare_execution(self) -> Dict[str, Any]:
        """No git operations needed for discussions"""

        return {
            'discussion_id': self.discussion_id,
            'work_dir': self.get_working_directory()
        }

    async def finalize_execution(
        self,
        result: Dict[str, Any],
        commit_message: str
    ) -> Dict[str, Any]:
        """No git finalization for discussions"""

        return {
            'success': True,
            'message': 'Discussions workspace requires no finalization'
        }

    async def post_output(
        self,
        agent_name: str,
        markdown_output: str
    ) -> Dict[str, Any]:
        """Post output as discussion comment"""

        from services.github_discussions import GitHubDiscussions

        discussions = GitHubDiscussions(
            self.github.repo_owner,
            self.github.repo_name,
            self.github.token
        )

        await discussions.add_comment(
            self.discussion_id,
            markdown_output
        )

        return {
            'success': True,
            'posted_to': f'discussion {self.discussion_id}'
        }

    def get_working_directory(self) -> Path:
        """Get temporary working directory for discussions"""
        # Discussions don't need a git repo, use temp workspace
        return Path(f"/tmp/discussions/{self.project}")

    async def get_execution_metadata(self) -> Dict[str, Any]:
        """Get metadata for observability"""
        return {
            'workspace_type': 'discussions',
            'discussion_id': self.discussion_id,
            'issue_number': self.issue_number
        }
```

### Usage in AgentExecutor (After Refactor)

```python
# services/agent_executor.py (SIMPLIFIED!)

class AgentExecutor:
    async def execute_agent(
        self,
        agent_name: str,
        project_name: str,
        task_context: Dict[str, Any],
        ...
    ) -> Dict[str, Any]:

        # Create workspace context (replaces all the conditionals!)
        workspace_ctx = WorkspaceContextFactory.create(
            workspace_type=task_context.get('workspace_type', 'issues'),
            project=project_name,
            issue_number=task_context['issue_number'],
            task_context=task_context,
            github_integration=self._get_github_integration(project_name)
        )

        # Prepare workspace (git branch OR discussion context)
        prep_result = await workspace_ctx.prepare_execution()
        task_context.update(prep_result)

        # Build execution context
        execution_context = await self._build_execution_context(
            agent_name=agent_name,
            project_name=project_name,
            task_id=task_id,
            task_context=task_context,
            stream_callback=stream_callback
        )

        # Create and execute agent
        agent_stage = self.factory.create_agent(agent_name, project_name)
        result = await agent_stage.execute(execution_context)

        # Post output to appropriate location
        markdown_output = self._extract_markdown_output(agent_name, result)
        await workspace_ctx.post_output(agent_name, markdown_output)

        # Finalize workspace (commit/PR OR no-op)
        commit_message = f"Complete work for issue #{task_context['issue_number']}"
        finalize_result = await workspace_ctx.finalize_execution(result, commit_message)

        # Log metadata
        metadata = await workspace_ctx.get_execution_metadata()
        logger.info(f"Agent {agent_name} completed: {metadata}")

        return result
```

## Benefits

### 1. Eliminates Conditionals
- **Before**: 100+ `if workspace_type == 'discussions'` checks
- **After**: Zero conditionals in orchestration layer

### 2. Single Responsibility
Each workspace context class handles ONE workspace type's behavior.

### 3. Easy to Extend
Want to add a new workspace type? Create a new class:
```python
class JiraWorkspaceContext(WorkspaceContext):
    # Implement interface for Jira integration
```

### 4. Testability
Easy to unit test each workspace type in isolation:
```python
def test_discussions_workspace_no_git():
    ctx = DiscussionsWorkspaceContext(...)
    assert ctx.supports_git_operations == False

def test_issues_workspace_creates_branch():
    ctx = IssuesWorkspaceContext(...)
    result = await ctx.prepare_execution()
    assert 'branch_name' in result
```

### 5. Type Safety
```python
# Clear contract - no guessing what happens
context: WorkspaceContext = factory.create(...)
assert hasattr(context, 'prepare_execution')
assert hasattr(context, 'finalize_execution')
```

## Migration Path

### ✅ Phase 1: Create Abstraction Layer (COMPLETED)
1. ✅ Create `services/workspace/` directory
2. ✅ Implement base `WorkspaceContext` class
3. ✅ Implement `IssuesWorkspaceContext`
4. ✅ Implement `DiscussionsWorkspaceContext`
5. ✅ Implement `HybridWorkspaceContext`
6. ✅ Implement `WorkspaceContextFactory`

**Status**: Complete - All workspace contexts implemented and tested (21/21 tests passing)

### ✅ Phase 2: Refactor AgentExecutor (COMPLETED)
1. ✅ Replace conditional logic with workspace context
2. ✅ Add tests for new abstraction
3. ✅ Verify backward compatibility

**Status**: Complete - AgentExecutor refactored (74 lines → 32 lines), orchestrator running successfully

### 🔄 Phase 3: Refactor Other Services (READY TO BEGIN)
1. ⏳ Review cycle manager (25+ conditional checks)
2. ⏳ Human feedback loop (10+ conditional checks)
3. ⏳ Conversational session manager (5+ conditional checks)
4. ⏳ Project monitor (30+ conditional checks)

**Status**: Ready - Can be done incrementally without breaking changes

### ⏳ Phase 4: Cleanup (PENDING)
1. ⏳ Remove old conditional logic from migrated services
2. ⏳ Update documentation
3. ⏳ Remove `workspace_type` parameters from method signatures

**Status**: Pending - Will be done after Phase 3 completes

---

## Implementation Status

**Completed**: 2025-10-07
- Phase 1: ✅ Complete
- Phase 2: ✅ Complete
- Phase 3: 🔄 Ready to begin
- Phase 4: ⏳ Pending

**Current State**: Production ready
- Orchestrator running successfully with new abstraction
- 21/21 tests passing
- Zero breaking changes
- Backward compatibility maintained

See `docs/WORKSPACE_REFACTOR_COMPLETE.md` for detailed implementation summary.

## File Structure

```
services/
├── workspace/
│   ├── __init__.py
│   ├── context.py              # Abstract base class + factory
│   ├── issues_context.py       # Issues workspace implementation
│   ├── discussions_context.py  # Discussions workspace implementation
│   └── hybrid_context.py       # Hybrid workspace implementation
├── agent_executor.py           # Simplified orchestration
├── review_cycle.py             # Can use workspace contexts
└── human_feedback_loop.py      # Can use workspace contexts
```

## Example: Review Cycle Refactor

### Before (Complex Conditionals)
```python
# review_cycle.py
if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
    await github.discussions.add_comment(...)
elif cycle_state.workspace_type == 'issues':
    await github.post_comment(...)
    if has_changes:
        await git.commit(...)
```

### After (Clean Abstraction)
```python
# review_cycle.py
workspace_ctx = WorkspaceContextFactory.create(
    workspace_type=cycle_state.workspace_type,
    ...
)

await workspace_ctx.post_output(agent_name, output)

if workspace_ctx.supports_git_operations:
    await workspace_ctx.finalize_execution(result, commit_msg)
```

## Conclusion

This abstraction layer reduces complexity from O(n × m) where n=features and m=workspace types to O(n + m) by:
- Centralizing workspace-specific logic in dedicated classes
- Eliminating scattered conditionals
- Making the codebase more maintainable and extensible
- Improving testability

The strategy pattern is perfect here because:
1. We have multiple algorithms (workspace types) for the same operations
2. The algorithms are well-defined and unlikely to change frequently
3. We want to eliminate conditionals
4. We want clean extension points
