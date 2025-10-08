"""
Workspace abstraction layer for handling different execution environments.

This package provides a clean abstraction over workspace-specific operations,
eliminating conditional logic throughout the codebase.
"""

from .context import WorkspaceContext, WorkspaceContextFactory
from .issues_context import IssuesWorkspaceContext
from .discussions_context import DiscussionsWorkspaceContext
from .hybrid_context import HybridWorkspaceContext

__all__ = [
    'WorkspaceContext',
    'WorkspaceContextFactory',
    'IssuesWorkspaceContext',
    'DiscussionsWorkspaceContext',
    'HybridWorkspaceContext',
]
