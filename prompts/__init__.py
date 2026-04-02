"""
Prompt management package for orchestrator agents.

Public API:
    PromptContext       — structured input data model
    IssueContext        — issue fields
    ReviewCycleContext  — review cycle state
    PromptBuilder       — assembles prompts from context + content files
"""

from prompts.context import PromptContext, IssueContext, ReviewCycleContext
from prompts.builder import PromptBuilder

__all__ = [
    "PromptContext",
    "IssueContext",
    "ReviewCycleContext",
    "PromptBuilder",
]
