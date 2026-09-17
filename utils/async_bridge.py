"""
Run a coroutine to completion from synchronous code, on or off the event loop.

`asyncio.run()` raises "cannot be called from a running event loop" when the
calling thread already has one. Several sync methods in this codebase are
reachable from BOTH contexts, which makes a bare `asyncio.run()` a latent crash
rather than an obvious one:

    services/project_monitor.py's trigger_agent_for_status() is the canonical
    dispatch entry point. The monitor reaches it on a daemon thread with no
    loop (threading.Thread(target=monitor_projects) in main.py), so
    asyncio.run() works -- which is why the bug below stayed hidden. But
    main.py's startup lock-recovery calls the same method directly from
    `async def main()`, i.e. ON the loop, and there every asyncio.run() under
    it raises.

That is not hypothetical: on 2026-09-17 a startup recovery for a closed issue
hit `_check_pr_ready_on_issue_exit` and logged

    CRITICAL: Failed to check PR ready on issue exit for #1017:
    asyncio.run() cannot be called from a running event loop

so the PR-ready check silently did not run. One occurrence in five days of
retained logs, because it needs a recovered lock whose issue is already closed.

TAKES A FACTORY, NOT A COROUTINE, and that is the whole point of the signature.
Both traps below are documented at project_monitor.py's resolve_workspace()
call site, having been found in review there:

  * The "is a loop running" probe must be isolated. `asyncio.get_running_loop()`
    and the coroutine can both raise plain RuntimeError, so a single try/except
    around both catches a genuine failure from the coroutine and then re-raises
    it as a confusing asyncio complaint from the fallback path. This is the
    load-bearing half: with the probe isolated, exactly one branch ever runs.
  * A coroutine object can only be awaited once, so the factory exists to make
    the reuse bug unconstructible rather than merely absent. Be precise about
    why, because it is easy to overclaim: given the isolated probe above, a
    single prebuilt coroutine would work fine today -- a mutation proves it,
    surviving the whole suite. The two defects only bite TOGETHER, which is
    exactly the shape that shipped: one try around both, one coroutine reused,
    so a RuntimeError from the call fell into the fallback, which re-awaited a
    consumed coroutine and reported "cannot reuse already awaited coroutine"
    while the real cause vanished. Taking the coroutine as a factory means a
    future refactor back to a single try cannot resurrect that pairing.

Exceptions from the coroutine propagate unchanged, so callers keep their own
error handling. Note the on-loop branch BLOCKS the calling loop until the
coroutine finishes -- acceptable only for bounded work. Do not use it for
anything that waits on a resource lock: an in-process holder releasing from a
coroutine on the very loop being blocked can never run, so the wait cannot
succeed. resolve_workspace()'s call site keeps its own inline copy of this
pattern for exactly that reason -- it has to pass a zero lock timeout that only
makes sense there -- as does the parent-issue lookup at project_monitor.py:2384,
which predates this helper.
"""

import asyncio
import concurrent.futures
from typing import Any, Callable, Coroutine


def run_coroutine_blocking(make_coro: Callable[[], Coroutine]) -> Any:
    """Await `make_coro()` to completion, whether or not a loop is running.

    Args:
        make_coro: zero-argument callable returning a NEW coroutine each call.
            Pass a lambda or functools.partial, never a coroutine object.

    Returns:
        Whatever the coroutine returns.
    """
    try:
        asyncio.get_running_loop()
        has_running_loop = True
    except RuntimeError:
        has_running_loop = False

    if has_running_loop:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, make_coro()).result()

    return asyncio.run(make_coro())
