"""Whether a pipeline run still in flight owns a given epic worktree.

Its own module, and a stdlib-only one, for a reason that is not style. The
answer is produced by `ActiveRunWorkspaces` in `services.pipeline_run` -- a
module that imports redis and elasticsearch at import time -- and consumed by
`services.project_workspace`, which must be able to name the UNKNOWN answer
even in the one failure mode where `services.pipeline_run` will not import at
all.

`survey_epic_worktrees()` backs an HTTP handler and promises never to raise. It
imports the run reader lazily inside a try, because a circular import during
startup or a missing transitive dependency in the operator script's environment
is a real failure shape there. When that import fails, Python has already
dropped the half-built module from `sys.modules`, so importing it AGAIN inside
the `except` -- to name the value meaning "could not ask" -- re-raises the same
error out of a method that promises it never raises. That is why the survey
used to carry `None` for "the module would not import" and a separate `bool`
for the match, i.e. the very gate-before-match split issue #240 exists to
remove.

Importing THIS module costs nothing but `enum`, so `project_workspace` can bind
`RunOwnership` at module load and still answer honestly when `pipeline_run`
cannot be reached.
"""

from enum import Enum


class RunOwnership(Enum):
    """The closed set of answers to "does a live pipeline run own this worktree".

    Exists so "I could not tell" cannot be spelled the same way as "nothing owns
    it". The consumer DELETES DIRECTORIES on this answer, and the bool it
    replaces returned False for both, which put the burden of gating on every
    call site and made forgetting the gate a silent `rm -rf` (#240).

    UNOWNED is the ONLY removable answer, so the removal site reads

        if ownership is not RunOwnership.UNOWNED:
            continue

    and stays correct even if an outer gate is later removed -- the outer gates
    remain for what only they can do (abort the whole sweep with one log line),
    not for correctness.

    There is deliberately ONE unknown member rather than the UNKNOWN_ALL /
    UNKNOWN_PROJECT pair sketched in #240, even though #233 has since given the
    per-project unknown a producer (`ActiveRunWorkspaces.projects_with_
    unaccountable_runs`, raised when an issue->run mapping entry names a run
    neither store can account for). Splitting the member would buy nothing a
    consumer acts on: every one of them branches on `is not UNOWNED`, both
    unknowns are equally unremovable, and both would render as the same
    'unknown' prune verdict. What an operator needs in order to tell the two
    apart -- which store failed, or which project's mapping holds debris -- is
    the lookup's own log line, where the run ids are, not a wider enum every
    call site would have to learn.

    Adding a narrower unknown later stays purely additive for the same reason:
    a new member fails closed by construction.
    """

    OWNED = 'owned'
    UNOWNED = 'unowned'
    UNKNOWN = 'unknown'
