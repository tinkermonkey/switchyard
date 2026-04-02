"""
Thin helper for loading workflow prompt templates in scripts/.

Scripts add the orchestrator root to sys.path so they can import from
prompts.loader directly; this helper provides a one-call convenience
wrapper to keep call sites clean.
"""

from prompts.loader import default_loader


def load_prompt(workflow_path: str, **kwargs: str) -> str:
    """Load a workflow template and format it with the given keyword arguments.

    workflow_path is relative to prompts/content/workflows/, without .md extension.
    If no kwargs are given the raw template text is returned.
    """
    template = default_loader.workflow_template(workflow_path)
    return template.format(**kwargs) if kwargs else template
