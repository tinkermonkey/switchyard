"""The Python source files that belong to THIS checkout (#221).

Four repo-wide guards -- the unbounded-FileHandler scan in
tests/unit/test_log_rotation.py, the hand-rolled-root grep in
tests/unit/test_state_root_all_doors.py, and the two #181/#203 AST walks in
tests/unit/test_state_root_isolation.py -- each walked `root.rglob('*.py')`
behind its OWN tuple of directory names to skip. The three tuples had already
drifted:

    test_log_rotation.py        ('tests', '.claude', 'node_modules', 'venv', '.venv')
    test_state_root_all_doors.py  + '.git'
    test_state_root_isolation.py  + '.git', 'orchestrator_data', 'state'

A denylist of names cannot express the property those guards actually want,
which is "a file of this repository". Anything else parked under the root --
a second checkout, a vendored tree, a scratch copy -- was scanned as if it
were first-party code. That is not hypothetical: an agent measuring a
before/after put an `origin/main` worktree at `.baseline-main/` inside the
tree it was standing in and all four guards failed, naming files nobody had
written and rules nobody had broken. A tripwire whose first observed firing is
a false positive is a tripwire that gets muted, which is the opposite of what
#202/#203 added these for.

## Why the walk still walks, rather than asking git

`git ls-files '*.py'` excludes a nested checkout by construction and is a
one-liner, and the issue suggests it is probably right. It is not, and the
reason is measurable rather than aesthetic: **it also excludes untracked
files**, and an untracked file is exactly the case these guards are for.

Measured on this branch before the fix, by dropping one untracked module into
services/ that constructs a `logging.FileHandler`, resolves ORCHESTRATOR_ROOT
by hand, hangs a `state/` path off `Path(__file__)`, and assigns
`os.environ['ORCHESTRATOR_ROOT']`:

    filesystem walk (today, and this module):  all four guards fail, naming it
    git ls-files '*.py':                       file absent; all four pass

A module written five minutes ago and not yet `git add`ed is the single most
likely place for a fresh #181/#203 violation to be sitting, and it is the
moment at which telling the author costs least. Handing that case to git would
have made every one of these guards quietly weaker while looking tidier.

So: keep the filesystem walk, and cut the nested checkouts out of it directly
-- refuse to descend into any directory that holds a `.git` entry. That entry
is a directory in a primary checkout and a FILE in a linked worktree (which is
what `.baseline-main/` was), so the test is `exists()`, not `is_dir()`.

## What is still a name list

Two, in one place: trees of this checkout that are not its source
(NOT_FIRST_PARTY_TREES, top level only) and dependency trees (VENDORED_TREES,
any depth). Measured deltas against each of the three tuples they replace:

* On a clean checkout of this branch, all three old tuples and this module
  enumerate the SAME 158 files. Nothing left scope.
* On the live deployment checkout, the old tuples enumerate 159 and this
  module 158. The one file that left is
  `web_ui/node_modules/flatted/python/flatted.py` -- a vendored dependency in
  a `node_modules/` that is not at the top level, which is what the old
  `parts[0] in (...)` test could not see.
* `orchestrator_data/` and `state/` come from the widest of the three old
  tuples. Adding them to the other two narrows nothing: 0 `*.py` under either,
  measured in this checkout and in the running deployment at /app.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Top-level trees of this checkout that are not this checkout's source. Only
# at the top level: `services/state/` would be source, `state/` is not.
NOT_FIRST_PARTY_TREES = frozenset({
    'tests',              # the guards are about the modules under test
    '.claude',            # agent/skill definitions, and the agent worktrees below
    '.git',               # git's own tree (a directory here; a file in a worktree)
    'orchestrator_data',  # gitignored runtime output
    'state',              # gitignored runtime state
})

# Dependency trees, wherever they sit. Not top-level-only, because they are not
# first-party at any depth and in this repo they are not all at the top:
# measured on the live deployment checkout, `web_ui/node_modules/flatted/
# python/flatted.py` is a vendored third-party module that every one of the
# three old top-level-only tuples scanned as if this repo had written it. It
# happens to break none of the four rules today, which is the only reason
# nobody has seen it fail.
VENDORED_TREES = frozenset({'node_modules', 'venv', '.venv'})


def is_nested_checkout(directory):
    """True if `directory` is the root of some OTHER git checkout.

    `.git` is a directory in a primary checkout and a file containing
    `gitdir: ...` in a linked worktree, so this asks only whether the entry
    exists.
    """
    return (Path(directory) / '.git').exists()


def first_party_python_sources(root=REPO_ROOT):
    """Every `*.py` file of this checkout, as sorted (relative, absolute) pairs.

    Untracked files are included on purpose -- see the module docstring.
    Excluded trees are PRUNED from the walk rather than filtered out of its
    results, so nothing inside one is stat'ed: on the live checkout that is the
    difference between visiting 4,805 `*.py` files and 158, `.venv/`'s 3,255
    and `.claude/worktrees/`'s 1,136 being most of the gap. Those first and
    third figures are a point-in-time reading (2026-09-13) and drift with the
    agent worktrees that happen to exist under `.claude/worktrees/` at the
    time; `.venv/`'s does not. Re-measure rather than trusting them -- the
    order of magnitude is the point, not the digits.
    """
    root = Path(root)
    found = []
    for directory, subdirectories, filenames in os.walk(root):
        here = Path(directory)
        relative_directory = here.relative_to(root)
        at_the_top = relative_directory == Path('.')
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in VENDORED_TREES
            and not (at_the_top and name in NOT_FIRST_PARTY_TREES)
            and not is_nested_checkout(here / name)
        ]
        for filename in filenames:
            if filename.endswith('.py'):
                found.append((relative_directory / filename, here / filename))
    return sorted(found)
