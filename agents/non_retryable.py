"""
Non-retryable agent error.

Raised when an agent encounters a permanent failure that will not resolve
on retry (e.g., cycle limits, missing prerequisites).  Both the inner
retry loop in agent_executor.py and the outer retry loop in worker_pool.py
recognise this exception and skip retries.
"""


class NonRetryableAgentError(RuntimeError):
    """Agent error that should not be retried."""
    pass
