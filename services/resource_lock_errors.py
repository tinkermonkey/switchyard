"""
Resource-lock timeout identification.

Single choke point for the question "is this exception a project-scoped
resource lock timeout?" -- i.e. ProjectCheckoutLockTimeoutError
(services/project_checkout_lock.py) or DevContainerBuildLockTimeoutError
(services/dev_container_build_lock.py).

Why this exists (#148, covering #140 items 8/10/13/25)
--------------------------------------------------------
A lock timeout is NOT an agent failure. Both lock modules deliberately fail
loudly rather than proceed unlocked (see project_checkout_lock.py's "Blocking
vs failing" section), and both are calibrated to outlast the longest
legitimate holder -- ~3h for project_checkout, ~1h for dev_container_build.
So by the time one of these is raised, the guarded operation never ran, and
the only thing that changes the outcome is a *different* holder finishing.
Every layer that reacts to an exception by retrying it, or by counting it
against a failure budget, therefore has to be able to recognise it:

  * services/agent_executor.py's retry loop would otherwise re-run the whole
    acquisition with the default retries=2, turning one genuine contention
    event into ~9h of wall clock before the pipeline finally fails.
  * services/circuit_breaker.py would otherwise count each such attempt
    against that agent+project's own breaker, so pure contention could trip
    it (failure_threshold=3) and block ALL further dispatch of that agent for
    that project for recovery_timeout on top of the contention itself.

Both those call sites already special-case ClaudeCodeRateLimitError for
exactly the same reason ("systemic condition, not this stage's fault"); this
module is the equivalent for lock contention, kept in one place rather than
copy-pasted so a third such call site can't get it subtly wrong (the same
concern #140 item 4 raised about the lock-acquisition guard pattern itself).

Why the __cause__ chain is walked
-----------------------------------
Item 25 of the same review: agents re-wrap whatever run_claude_code() raises
into a plain `Exception(...) from exc`, erasing the type before any of the
above can see it. agents/base_maker_agent.py's wrapper is fixed at the source
(mirroring the CancellationError/ClaudeCodeRateLimitError precedent already
there), but it is not the only one -- agents/code_reviewer_agent.py and
agents/documentation_editor_agent.py carry the identical wrapper, and nothing
stops a fourth from being written. Walking `__cause__` makes recognition
robust to all of them without every wrapper having to remember. Only
`__cause__` is followed, never `__context__`: `raise X from exc` is a
deliberate statement that X *is* exc re-expressed, whereas `__context__` is
set implicitly by any exception merely raised while another was being
handled, which would produce false positives.

Imports are deferred into the helpers below so that low-level callers
(services/circuit_breaker.py, reached from pipeline/base.py's constructor)
don't take a module-import dependency on the lock stack and its Redis-backed
manager -- the same deferred-import reasoning circuit_breaker.py already
applies to monitoring/claude_code_breaker.
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Bounds the __cause__ walk. Real chains are one or two links deep; this only
# exists so a pathological (or deliberately cyclic) chain can't spin here.
_MAX_CAUSE_DEPTH = 10

_timeout_error_types: Optional[Tuple[type, ...]] = None


def lock_timeout_error_types() -> Tuple[type, ...]:
    """
    The resource-lock timeout exception types, as a tuple suitable for
    isinstance()/except. Deferred (and then cached) import -- see this module's
    docstring.

    Every caller is inside an `except` block handling somebody else's
    exception, so an import failure here must never become the exception that
    propagates in its place: returning an empty tuple degrades to the
    pre-#148 behavior (the lock timeout is treated as an ordinary failure)
    instead of masking the real error with an ImportError.
    """
    global _timeout_error_types

    if _timeout_error_types is None:
        try:
            from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
            from services.dev_container_build_lock import DevContainerBuildLockTimeoutError
        except Exception as import_error:
            logger.warning(
                f"Could not load the resource-lock timeout types "
                f"({type(import_error).__name__}: {import_error}) -- lock timeouts "
                f"will be treated as ordinary failures until this is resolved"
            )
            return ()
        _timeout_error_types = (ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError)

    return _timeout_error_types


def _find_lock_timeout(exc: BaseException) -> Optional[BaseException]:
    """
    The lock timeout that `exc` is, or that it explicitly re-wraps via
    `raise ... from <lock timeout>` -- None if there isn't one. Single
    implementation of the walk, shared by both public helpers below so a
    change to one can't silently diverge from the other.
    """
    if exc is None:
        return None

    timeout_types = lock_timeout_error_types()

    seen = set()
    current: Optional[BaseException] = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return None
        if isinstance(current, timeout_types):
            return current
        seen.add(id(current))
        current = current.__cause__

    return None


def is_lock_timeout_error(exc: BaseException) -> bool:
    """
    True if `exc` is a resource-lock timeout, or explicitly re-wraps one via
    `raise ... from <lock timeout>` (see this module's docstring for why the
    __cause__ chain is followed and __context__ is not).
    """
    return _find_lock_timeout(exc) is not None


def describe_lock_timeout(exc: BaseException) -> str:
    """
    Short "<TypeName>: <message>" rendering of the lock timeout inside `exc`
    (the exception itself, or whatever wrapper explicitly chained it), for log
    lines that want to name the actual contention rather than the wrapper's
    generic text. Falls back to `exc` itself if no lock timeout is found.
    """
    found = _find_lock_timeout(exc)
    if found is not None:
        return f"{type(found).__name__}: {found}"
    return f"{type(exc).__name__}: {exc}"
