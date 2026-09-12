"""No state path may be derived from where the code happens to live (#181).

The documented way to run this suite is `pytest tests/unit` from the
repository root. On the deployment that root IS the directory bind-mounted at
`/app`, so any module resolving its state directory from
`Path(__file__).parent.parent` wrote the suite's fixtures into the LIVE state
tree -- and the production watchdog then did real work on them. Seventeen files
were observed reappearing after a verified-clean deletion, every one
timestamped to a test run rather than to the orchestrator.

Eight modules already read ORCHESTRATOR_ROOT. Two did not, and those two were
the hole.
"""

import ast
import importlib
import os
import re
import sys
import types
from pathlib import Path

import pytest

from config.state_manager import orchestrator_state_root


class TestTheResolver:

    def test_orchestrator_root_wins_when_set(self, monkeypatch):
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '/scratch/elsewhere')
        assert orchestrator_state_root() == Path('/scratch/elsewhere/state')

    def test_it_falls_back_to_the_checkout_when_unset(self, monkeypatch):
        """Unchanged production behaviour: with nothing set, the deployment
        still resolves its own `state/`. The fix adds an override, it does not
        move anything by default."""
        monkeypatch.delenv('ORCHESTRATOR_ROOT', raising=False)
        import config.state_manager as sm

        expected = Path(sm.__file__).parent.parent / 'state'
        assert orchestrator_state_root() == expected

    def test_an_empty_value_is_treated_as_unset(self, monkeypatch):
        """`-e ORCHESTRATOR_ROOT=` on a docker run passes an empty string.
        Reading that as a root would resolve every state path to `/state`."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '')
        import config.state_manager as sm

        assert orchestrator_state_root() == Path(sm.__file__).parent.parent / 'state'


class TestBothHoldoutsUseIt:
    """The two modules that derived from __file__ instead."""

    def test_github_state_manager_follows_the_override(self, monkeypatch, tmp_path):
        """Constructed directly, NOT via a module reload.

        An earlier version called importlib.reload here.
        `config/state_manager.py` ends with `state_manager = GitHubStateManager()`
        at module scope, so a reload rebinds that process-wide singleton to a
        root under tmp_path -- which pytest then deletes -- and mints a second
        class object, so `isinstance` and
        `patch('config.state_manager.GitHubStateManager')` in any later test
        stop referring to what their consumers actually use. 26 non-test call
        sites do a function-level `from config.state_manager import
        state_manager` -- 20 of them in services/project_monitor.py alone --
        and would pick up the new one while module-level importers kept the
        old.

        It was never needed: orchestrator_state_root() reads the environment on
        every call, so constructing under the patched env proves the same thing
        with no session-wide side effect.
        """
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        import config.state_manager as sm

        manager = sm.GitHubStateManager()

        assert manager.state_root == tmp_path / 'state'
        assert manager.projects_state_dir == tmp_path / 'state' / 'projects'

    def test_pr_review_state_manager_follows_the_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        import state_management.pr_review_state_manager as prs

        manager = prs.PRReviewStateManager()

        assert manager.state_root == tmp_path / 'state' / 'projects'

    def test_the_module_level_singleton_honours_the_override_too(self):
        """The singleton is the call site that actually matters.

        `config/state_manager.py` builds one at import time, and
        services/pipeline_progression.py, claude/docker_runner.py,
        agents/orchestrator_integration.py and main.py all reach for it. Both
        tests above construct fresh managers, which would pass even if the
        singleton had been built before the redirect took effect.
        """
        import config.state_manager as sm

        assert sm.state_manager.state_root == orchestrator_state_root(), (
            f"singleton={sm.state_manager.state_root} "
            f"resolver={orchestrator_state_root()} "
            f"env={os.environ.get('ORCHESTRATOR_ROOT')!r}"
        )

    def test_an_explicit_state_root_still_beats_the_environment(self, monkeypatch, tmp_path):
        """Callers that pass a root mean it.

        No non-test caller passes `state_root=` today --
        scripts/dry_run_state_sweep.py mutates the existing singleton in place
        instead, deliberately, because importers hold that object. The
        parameter is still part of the contract and two test files rely on it.
        """
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '/ignored')
        import config.state_manager as sm

        manager = sm.GitHubStateManager(state_root=str(tmp_path / 'chosen'))

        assert manager.state_root == tmp_path / 'chosen'


# ---------------------------------------------------------------------------
# The #181 invariant, as an AST walk rather than a source grep (#203)
# ---------------------------------------------------------------------------
#
# What replaced what, and why. The previous guard scanned source text ONE LINE
# AT A TIME, matching either `__file__` and `state` on the same line or a
# path-shaped quoted literal. Both shapes of #181 survive a line break, and
# `<var> / "state"` -- the commonest spelling in this tree -- it could not see
# at ALL. Its own EXEMPT table carried an entry for mcp/server.py that matched
# nothing, added by someone who believed `APP_ROOT / "state" / "projects"` was
# covered. It was not, and the deadness of that entry was the measure of the
# hole.
#
# Measured against origin/main rather than quoted: on that tree the line scan
# reports ZERO offenders, while this walk reports thirteen sites in five
# first-party files (mcp/server.py and four scripts/) that build a state path
# from a root the resolver never saw. Those thirteen feed eight mkdir calls and
# six file writes. #203 fixes all thirteen.
#
# So: parse each module, find every expression that builds a path with a
# `state` segment in it, trace that expression's ROOT back through local name
# bindings, and require the root to be `orchestrator_state_root()` -- or one of
# the roots explicitly allowed for that file below, each with its reason.
#
# WHAT THIS DELIBERATELY DOES NOT DO, so the name does not outrun the code:
#
#   * It is intraprocedural and per-module. A root that arrives as a function
#     parameter, or from a helper in another module, is opaque -- it is
#     reported by the name of whatever it came from, which fails closed, but
#     the guard cannot tell you what that helper resolves to. Same for a
#     renamed constructor: `from pathlib import Path as _P` is not in
#     _PATH_TYPES, so `_P(__file__).parent.parent / "state"` is reported with
#     the root `call:_P()` rather than `__file__` -- measured, and the point is
#     that it is still reported.
#   * It does not flag writes that are merely relative to `__file__` without
#     naming `state` (e.g. `Path(__file__).parent / "cache"`). That is a real
#     but different hazard and this guard would have to claim more than it
#     checks to say it covers it.
#   * Branches are UNIONED, not narrowed: if a name is bound to a safe root on
#     one line and an unsafe one on another, the unsafe one is reported. That
#     is the intended direction -- a guard that resolves branches would start
#     missing things.
#   * Of the ways to assemble a path out of strings, it knows `/`, `+`,
#     f-strings, `.format()`, `%`-formatting, `os.path.join()`, and
#     `<sep>.join([...])` where <sep> is `os.sep`, `os.path.sep` or a literal
#     separator. It does NOT know the rest -- measured misses:
#     `''.join([base, '/state'])`, `SEP.join([base, 'state'])` with the
#     separator in a variable, and any `bytes` path such as
#     `os.makedirs(b'/app/state')`. This list is the second draft -- the
#     first named neither `%`-formatting nor separator joins nor
#     loop-accumulated segments, all
#     three of which walked past it, and all three of which are now both fixed
#     and pinned as samples in _DEFEATED_THE_FIRST_DRAFT_OF_THIS_WALK below.
#     Treat the list as the measured edge of the walk, not as a boundary that
#     was reasoned about once and then trusted.
#   * A loop variable is bound to the ELEMENTS of a literal sequence and
#     otherwise to the iterable expression itself. `for seg in segments_from(x)`
#     is therefore opaque in the same way a function parameter is.

_STATE_SEGMENT = re.compile(r'(?:^|/)state(?:/|$)')

_PATH_TYPES = {'Path', 'PurePath', 'PosixPath', 'PurePosixPath'}
# os.path helpers whose FIRST argument is the path being operated on
_OS_PATH_ROOTED = {'join', 'dirname', 'abspath', 'realpath', 'expanduser',
                   'normpath', 'relpath'}
# methods returning a path derived from their receiver (str methods included:
# `(os.environ.get(...) or '').strip()` is a root in config/state_manager.py)
_PATH_DERIVING = {'joinpath', 'resolve', 'expanduser', 'absolute', 'with_name',
                  'with_suffix', 'strip', 'rstrip', 'lstrip', 'format'}
_PATH_ATTRS = {'parent', 'parents'}
# receiver-style writers: `p.mkdir()`, `p.write_text()`, ...
_WRITE_METHODS = {'mkdir', 'write_text', 'write_bytes', 'touch', 'unlink',
                  'rmdir', 'replace', 'rename', 'open'}
# argument-style writers: `open(p, 'w')`, `os.makedirs(p)`, `shutil.rmtree(p)`
_WRITE_FUNCS = {'open', 'makedirs', 'mkdir', 'rmtree', 'remove', 'unlink',
                'copy', 'copy2', 'copytree', 'move'}

# separators that make `<sep>.join([...])` a PATH join rather than prose
_PATH_SEPARATORS = {'/', '\\'}
_PATH_SEP_NAMES = {'os.sep', 'os.path.sep', 'sep'}
# a %-format string that STARTS with its first placeholder: '%s/state' puts the
# substituted value at the front of the path, '/app/state/%s' does not
_LEADS_WITH_PLACEHOLDER = re.compile(
    r'^%(?:\([^)]*\))?[-+ #0]*[0-9*]*(?:\.[0-9*]+)?[hlL]?[a-zA-Z%]'
)

SAFE_ROOT = 'orchestrator_state_root()'


def _dotted(node):
    """'os.path.join' for an Attribute/Name chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return '.'.join(reversed(parts))
    return None


def _path_join_elements(node):
    """The pieces of `<sep>.join([...])`, or None if this is not a path join.

    `os.path.join(...)` is already handled as an _OS_PATH_ROOTED call. This is
    the OTHER spelling -- `os.sep.join([base, 'state'])`, `'/'.join([...])` --
    which the first draft of this walk missed entirely, because `.join` takes a
    SEQUENCE and segments() had no case for a list display.

    Gated on the separator rather than on the bare method name, and the gate is
    load-bearing: measured, with it removed `', '.join(['state', 'status'])`
    is reported as a state path rooted at `literal:'state'`. (Nothing in this
    tree joins a literal list containing 'state' today -- the tree-wide check
    reports zero findings with the gate removed as well as with it in place --
    so the gate is protecting future code, not current code.)
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr != 'join' or len(node.args) != 1:
        return None
    receiver = node.func.value
    separator_named = _dotted(receiver) in _PATH_SEP_NAMES
    separator_literal = (isinstance(receiver, ast.Constant)
                         and receiver.value in _PATH_SEPARATORS)
    if not (separator_named or separator_literal):
        return None
    sequence = node.args[0]
    if isinstance(sequence, (ast.List, ast.Tuple, ast.Set)):
        return list(sequence.elts)
    return [sequence]


def _callee(node):
    if not isinstance(node, ast.Call):
        return ''
    return _dotted(node.func) or (
        node.func.attr if isinstance(node.func, ast.Attribute) else ''
    )


def _environ_key(node):
    """The variable name for `os.environ.get('X', ...)`, else None."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in ('get', 'setdefault'):
        return None
    if not (_dotted(node.func.value) or '').endswith('environ'):
        return None
    if node.args and isinstance(node.args[0], ast.Constant):
        return node.args[0].value
    return '?'


def _has_state_segment(segments):
    return any(_STATE_SEGMENT.search(s) for s in segments if isinstance(s, str))


class _Bindings:
    """One module's name -> value map, plus the three walks the guard needs.

    Flat and scope-blind on purpose: every assignment to a name anywhere in the
    module is a possible value for it. That over-approximates across functions,
    and it over-approximates in the safe direction -- a name that is ever bound
    to a bad root is reported wherever it is used.
    """

    def __init__(self, tree):
        self.by_name = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self._bind(target, node.value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                self._bind(node.target, node.value)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self._bind_iteration(node.target, node.iter)

    def _bind_iteration(self, target, iterable):
        """A loop variable is bound to whatever the sequence yields.

        `for seg in ['state', 'projects']: root = root / seg` is a real #181
        spelling, and the only reason the first draft of this walk missed it is
        that a for-target is not an Assign, so the loop variable had no binding
        and the `state` literal was never reachable from `root`.

        A literal sequence binds element by element. Anything else binds to the
        iterable expression itself, which over-approximates in the safe
        direction: segments() does not follow names, so a Name iterable
        contributes no segments of its own and only state_origin()'s binding
        walk can reach through it.
        """
        if isinstance(iterable, (ast.List, ast.Tuple, ast.Set)):
            for element in iterable.elts:
                self._bind(target, element)
        else:
            self._bind(target, iterable)

    def _bind(self, target, value):
        if isinstance(target, ast.Name):
            key = target.id
        elif isinstance(target, ast.Attribute):
            key = _dotted(target)  # `self.state_dir = ...`
        else:
            key = None
        if key:
            self.by_name.setdefault(key, []).append(value)

    def of(self, node):
        if isinstance(node, ast.Name):
            return self.by_name.get(node.id, [])
        if isinstance(node, ast.Attribute):
            return self.by_name.get(_dotted(node) or '', [])
        return []

    def segments(self, node, depth=0):
        """Literal path pieces this expression contributes ITSELF.

        Does not follow names -- state_origin() does that, so that the finding
        is reported at the line that built the path rather than at every later
        use of it.
        """
        if depth > 12:
            return []
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add, ast.Mod)):
            # Mod is `'%s/state' % base`. The f-string and `.format()` spellings
            # of the same path were already covered; %-formatting was not, and
            # nothing in the limits list said so.
            return (self.segments(node.left, depth + 1)
                    + self.segments(node.right, depth + 1))
        if isinstance(node, ast.JoinedStr):
            out = []
            for value in node.values:
                out += self.segments(value, depth + 1)
            return out
        if isinstance(node, ast.Attribute) and node.attr in _PATH_ATTRS:
            return self.segments(node.value, depth + 1)
        if isinstance(node, ast.Subscript):  # Path(...).parents[1]
            return self.segments(node.value, depth + 1)
        elements = _path_join_elements(node)
        if elements is not None:  # os.sep.join([base, 'state', 'projects'])
            out = []
            for element in elements:
                out += self.segments(element, depth + 1)
            return out
        if isinstance(node, ast.Call):
            base = _callee(node).split('.')[-1]
            if base in _PATH_TYPES or base in _OS_PATH_ROOTED or base in _PATH_DERIVING:
                out = []
                if isinstance(node.func, ast.Attribute) and base in _PATH_DERIVING:
                    out += self.segments(node.func.value, depth + 1)
                for arg in node.args:
                    out += self.segments(arg, depth + 1)
                return out
        return []

    def state_origin(self, node, seen=frozenset(), depth=0):
        """The expression that actually introduces the `state` segment, or None.

        Follows name bindings, so `STATE_DIR / project / 'github_state.yaml'`
        is reported at the line that built STATE_DIR.
        """
        if depth > 12 or id(node) in seen:
            return None
        seen = seen | {id(node)}
        if isinstance(node, (ast.Name, ast.Attribute)):
            if isinstance(node, ast.Attribute) and node.attr in _PATH_ATTRS:
                return self.state_origin(node.value, seen, depth + 1)
            for value in self.of(node):
                found = self.state_origin(value, seen, depth + 1)
                if found is not None:
                    return found
            return None
        if _has_state_segment(self.segments(node)):
            return node
        children = []
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add, ast.Mod)):
            children = [node.left, node.right]
        elif isinstance(node, ast.Subscript):
            children = [node.value]
        elif isinstance(node, ast.JoinedStr):
            children = list(node.values)
        elif isinstance(node, ast.FormattedValue):
            children = [node.value]
        elif _path_join_elements(node) is not None:
            children = _path_join_elements(node)
        elif isinstance(node, ast.Call):
            base = _callee(node).split('.')[-1]
            if base in _PATH_TYPES or base in _OS_PATH_ROOTED or base in _PATH_DERIVING:
                children = list(node.args)
                if isinstance(node.func, ast.Attribute) and base in _PATH_DERIVING:
                    children.append(node.func.value)
        for child in children:
            found = self.state_origin(child, seen, depth + 1)
            if found is not None:
                return found
        return None

    def roots(self, node, seen=frozenset(), depth=0):
        """Every atom this path expression could be anchored at.

        Walks the LEFT spine of `/` and `+`, unwraps `Path()`, `os.path.*`,
        `.parent`, `.parents[n]`, `.joinpath()`, `.resolve()` and friends, and
        follows names through every binding they have.
        """
        if depth > 16 or id(node) in seen:
            return []
        seen = seen | {id(node)}
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            return self.roots(node.left, seen, depth + 1) or [node.left]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            # `fmt % values`. Which side is the ROOT depends on the format
            # string: '%s/state' % base is anchored at base, '/app/state/%s' %
            # name is anchored at the literal. Getting this right is what makes
            # the finding say `__file__` instead of `literal:'%s/state'`; both
            # fail closed, only one is readable.
            left, right = node.left, node.right
            if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                    and _LEADS_WITH_PLACEHOLDER.match(left.value)):
                if isinstance(right, ast.Dict):  # '%(base)s/state' % {...}
                    out = []
                    for value in right.values:
                        out += self.roots(value, seen, depth + 1)
                    return out or [right]
                if isinstance(right, (ast.Tuple, ast.List)) and right.elts:
                    right = right.elts[0]  # substituted left to right
                return self.roots(right, seen, depth + 1) or [right]
            return self.roots(left, seen, depth + 1) or [left]
        if isinstance(node, ast.BoolOp):  # `os.environ.get(...) or ''`
            out = []
            for value in node.values:
                out += self.roots(value, seen, depth + 1)
            return out or [node]
        if isinstance(node, ast.JoinedStr):  # f"{BASE}/state"
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    return self.roots(value.value, seen, depth + 1) or [value.value]
                if isinstance(value, ast.Constant) and value.value:
                    return [value]
            return [node]
        if isinstance(node, ast.Subscript):
            return self.roots(node.value, seen, depth + 1) or [node.value]
        if isinstance(node, ast.Attribute):
            if node.attr in _PATH_ATTRS:
                return self.roots(node.value, seen, depth + 1) or [node.value]
            bound = self.of(node)
            if not bound:
                return [node]
            out = []
            for value in bound:
                out += self.roots(value, seen, depth + 1)
            return out or [node]
        if isinstance(node, ast.Call):
            if _environ_key(node) is not None:
                # Stop here: label() renders the variable AND its fallback, and
                # the fallback is the half that decides whether this is #181.
                return [node]
            elements = _path_join_elements(node)
            if elements:  # os.sep.join([base, ...]) is anchored at base
                return self.roots(elements[0], seen, depth + 1) or [elements[0]]
            base = _callee(node).split('.')[-1]
            if base in _PATH_DERIVING and isinstance(node.func, ast.Attribute):
                return self.roots(node.func.value, seen, depth + 1) or [node.func.value]
            if (base in _PATH_TYPES or base in _OS_PATH_ROOTED) and node.args:
                return self.roots(node.args[0], seen, depth + 1) or [node.args[0]]
            return [node]
        if isinstance(node, ast.Name):
            bound = self.of(node)
            if not bound:
                return [node]
            out = []
            for value in bound:
                out += self.roots(value, seen, depth + 1)
            return out or [node]
        return [node]

    def label(self, node, depth=0):
        """A stable, readable name for a root -- what EXEMPT entries are keyed on."""
        if isinstance(node, ast.Name):
            return '__file__' if node.id == '__file__' else f'name:{node.id}'
        if isinstance(node, ast.Constant):
            return f'literal:{node.value!r}'
        if isinstance(node, ast.Attribute):
            return f'attr:{_dotted(node) or node.attr}'
        if isinstance(node, ast.Call):
            key = _environ_key(node)
            if key is not None:
                if len(node.args) < 2:
                    return f'env:{key}'
                if depth > 4:
                    return f'env:{key}->...'
                fallback = node.args[1]
                inner = sorted({
                    self.label(root, depth + 1)
                    for root in (self.roots(fallback) or [fallback])
                })
                return f'env:{key}->' + '|'.join(inner)
            name = _callee(node) or '?'
            if name.split('.')[-1] == 'orchestrator_state_root':
                return SAFE_ROOT
            return f'call:{name}()'
        return type(node).__name__


def state_path_findings(source, relpath, allowed_roots=frozenset()):
    """Every state path in `source` whose root is neither the resolver nor allowed.

    One finding per line that CONSTRUCTS such a path, with the union of every
    root that line's expression can be anchored at.
    """
    tree = ast.parse(source)
    binds = _Bindings(tree)
    found = {}

    def consider(expr, fallback_line):
        origin = binds.state_origin(expr)
        if origin is None:
            return
        labels = sorted({binds.label(root) for root in (binds.roots(expr) or [expr])})
        if all(label == SAFE_ROOT or label in allowed_roots for label in labels):
            return
        line = getattr(origin, 'lineno', fallback_line)
        previous = found.get(line)
        merged = sorted(set(labels) | set(previous[1] if previous else ()))
        found[line] = (line, merged, ast.unparse(origin))

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            consider(node, node.lineno)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            # Gated exactly like the f-string below, and for the same reason:
            # `logger.info('wrote %s' % STATE_DIR)` interpolates a state path
            # without building one, and its root is the format string. Measured
            # on the shape that matters, an exempt file's own log line:
            # ungated, `os.environ.get('ORCHESTRATOR_ROOT', '/app') + '/state'`
            # followed by `logger.info('wrote %s' % STATE_DIR)` is reported with
            # the root `literal:'wrote %s'`, which the file's exemption cannot
            # name and which is not what built the path. The gate asks that the
            # FORMAT STRING itself carry the `state` segment.
            #
            # What that leaves flagged, deliberately and symmetrically with the
            # f-string: a log line whose format string spells out a state path,
            # `'%s/state/projects was rebuilt' % base_dir`. Measured -- the
            # f-string spelling of that same line is flagged today.
            if _has_state_segment(binds.segments(node)):
                consider(node, node.lineno)
        elif isinstance(node, ast.JoinedStr):
            # Only a path-shaped f-string. Without this gate every log line
            # that interpolates a state path gets reported at its own root,
            # which is the format string.
            if _has_state_segment(binds.segments(node)):
                consider(node, node.lineno)
        elif isinstance(node, ast.Call):
            base = _callee(node).split('.')[-1]
            if base in _PATH_TYPES or base in _OS_PATH_ROOTED or base in _PATH_DERIVING:
                consider(node, node.lineno)
            elif base in _WRITE_FUNCS and node.args:
                consider(node.args[0], node.lineno)
            elif base in _WRITE_METHODS and isinstance(node.func, ast.Attribute):
                consider(node.func.value, node.lineno)
        elif isinstance(node, ast.Assign):
            # `STATE_REL = "state/projects"` -- a path literal parked in a
            # constant. Requires a '/' so that `self.state = "running"` and
            # every status field in the codebase stay out of it.
            value = node.value
            if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                    and '/' in value.value and _has_state_segment([value.value])):
                consider(value, node.lineno)

    return [
        f'{relpath}:{line}: root {"|".join(labels)}: {code[:120]}'
        for line, labels, code in sorted(found.values())
    ]


# path -> (roots allowed IN THAT FILE, why). Not whole-file exemptions: a root
# not listed here still fails, in these files as in any other. The old table
# exempted services/data_retention.py -- the module that DELETES aged trees --
# whole-file; it needs no entry at all now, because its `relative_path=` values
# are relative SEGMENTS in a rule table, not path roots, and the AST walk can
# tell the difference. An absolute or __file__-derived path added to that file
# tomorrow is reported.
EXEMPT_ROOTS = {
    'config/state_manager.py': (
        frozenset({'__file__', 'env:ORCHESTRATOR_ROOT', "literal:''"}),
        "defines orchestrator_state_root(): the __file__ fallback IS the "
        "deployment's own behaviour when ORCHESTRATOR_ROOT is unset, and the "
        "env read plus its `or ''` are the resolver itself",
    ),
    'scripts/dry_run_state_sweep.py': (
        frozenset({
            'call:_resolve_deployment_root()',
            'call:tempfile.mkdtemp()',
            'attr:args.scratch',
            "literal:'/app/state'",
        }),
        "the dry-run harness: its whole contract is 'snapshot the LIVE state "
        "tree, copy it to scratch, run the sweep against the copy, prove the "
        "original is byte-identical'. It must name both roots by hand, and it "
        "deliberately does NOT use ORCHESTRATOR_ROOT for the live one because "
        "it overwrites that variable itself",
    ),
    # The seven modules that read ORCHESTRATOR_ROOT directly with an ABSOLUTE
    # default. #181 blessed this shape and did not change it; #203 does not
    # either. What the exemption is pinned to is the point: the label carries
    # the fallback, so `os.environ.get('ORCHESTRATOR_ROOT', '.')` or a
    # `__file__` fallback in one of these files is a DIFFERENT label and still
    # fails -- which is exactly how the five scripts #203 fixed were found.
    #
    # Stated plainly, because the docstring should not imply otherwise: this
    # shape is weaker than the resolver. It does not refuse a relative
    # ORCHESTRATOR_ROOT, so `ORCHESTRATOR_ROOT=tmp/scratch` still resolves
    # against the CWD in these seven. Migrating them to
    # orchestrator_state_root() is the obvious follow-up and is deliberately
    # not bundled into this change: they are the lock, queue, semaphore and
    # execution-history roots of a running deployment.
    **{
        path: (
            frozenset({"env:ORCHESTRATOR_ROOT->literal:'/app'"}),
            "reads ORCHESTRATOR_ROOT directly with an absolute /app fallback "
            "(pre-existing, see #181); pinned to that exact fallback",
        )
        for path in (
            'services/conversational_session_state.py',
            'services/dev_container_state.py',
            'services/pipeline_lock_manager.py',
            'services/pipeline_queue_manager.py',
            'services/pipeline_semaphore_manager.py',
            'services/scheduled_tasks.py',
            'services/work_execution_state.py',
        )
    },
}

_SKIP_TREES = ('tests', '.claude', 'node_modules', 'venv', '.venv', '.git',
               'orchestrator_data', 'state')


def _first_party_sources(root):
    for source in sorted(root.rglob('*.py')):
        relative = source.relative_to(root)
        if relative.parts[0] in _SKIP_TREES:
            continue
        yield relative, source


class TestNoModuleStillDerivesStateFromItsOwnLocation:

    def test_every_state_path_is_rooted_at_the_resolver_or_an_allowed_root(self):
        """The #181 invariant, checked as an invariant rather than as a grep.

        Replaces a line-at-a-time regex scan that reported zero offenders while
        thirteen sites in five files walked past it. See the module comment
        above for what it does and what it still does not.
        """
        root = Path(__file__).parent.parent.parent

        offenders = []
        for relative, source in _first_party_sources(root):
            allowed, _ = EXEMPT_ROOTS.get(str(relative), (frozenset(), ''))
            offenders += state_path_findings(
                source.read_text(errors='ignore'), relative, allowed
            )

        assert offenders == [], (
            "these lines build a `state/` path from a root the orchestrator's "
            "resolver never saw, which ignores ORCHESTRATOR_ROOT and writes "
            "into the deployment (#181):\n  " + "\n  ".join(offenders) +
            "\nUse config.state_manager.orchestrator_state_root(), or add the "
            "root to EXEMPT_ROOTS in this file with the reason it is safe."
        )

    def test_every_exemption_is_live(self):
        """No entry may be in EXEMPT_ROOTS without something to exempt.

        This is the test the old table needed and did not have. Its
        `mcp/server.py` entry matched NOTHING -- the patterns could not see
        `APP_ROOT / "state" / "projects"` -- so a reader checking whether the
        MCP server was covered found an exemption and concluded it was
        considered. Nothing had been.
        """
        root = Path(__file__).parent.parent.parent

        dead_files, dead_roots = [], []
        for relative, (allowed, reason) in sorted(EXEMPT_ROOTS.items()):
            assert reason, f"{relative} is exempt without a stated reason"
            source = root / relative
            assert source.is_file(), f"{relative} is exempt but does not exist"

            findings = state_path_findings(source.read_text(errors='ignore'), relative)
            if not findings:
                dead_files.append(str(relative))
                continue
            seen = {
                label
                for finding in findings
                for label in finding.split(': root ', 1)[1].split(': ', 1)[0].split('|')
            }
            for unused in sorted(allowed - seen):
                dead_roots.append(f"{relative}: {unused}")

        assert not dead_files, (
            "these files are exempt but the guard finds nothing in them -- the "
            "exemption is dead and says more than it does:\n  "
            + "\n  ".join(dead_files)
        )
        assert not dead_roots, (
            "these exempt roots match nothing in their file:\n  "
            + "\n  ".join(dead_roots)
        )


# Shapes that reintroduce #181. Split by what the PREDECESSOR did with them,
# measured, not assumed: the first five walk straight past the line scan, the
# last three it already caught. Keeping the caught ones here too is the
# regression half -- an AST walk that quietly stopped seeing `os.path.join(...,
# "state")` would be a downgrade even while looking like an upgrade.
_DEFEATED_THE_LINE_SCAN = {
    'split __file__ derivation from the literal': """
from pathlib import Path
_ROOT = Path(__file__).parent.parent
STATE_DIR = _ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
""",
    'parents[1] on one line, the join on the next': """
from pathlib import Path
root = Path(__file__).parents[1]
p = root / "state" / "projects"
p.mkdir(parents=True, exist_ok=True)
""",
    'os.path.dirname then string concatenation': """
import os
d = os.path.dirname(os.path.dirname(__file__))
x = d + "/state"
os.makedirs(x, exist_ok=True)
""",
    'f-string built from a module constant': """
BASE = "/app"
STATE_ROOT = f"{BASE}/state"
""",
    '<var> / "state" with an env read and a /app default': """
import os
from pathlib import Path
APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
STATE_DIR = APP_ROOT / "state" / "projects"
""",
}

_THE_LINE_SCAN_ALREADY_CAUGHT = {
    'joinpath instead of the / operator': """
from pathlib import Path
p = Path(__file__).parent.parent.joinpath("state")
p.mkdir(parents=True, exist_ok=True)
""",
    'os.path.join with the segments split out': """
import os
p = os.path.join(os.path.dirname(__file__), "state", "projects")
os.makedirs(p, exist_ok=True)
""",
    'a relative literal parked in a constant': """
from pathlib import Path
STATE_REL = "state/projects"
Path(STATE_REL).mkdir(parents=True, exist_ok=True)
""",
}

# Shapes that defeated the FIRST DRAFT of this AST walk -- found in review of
# #203, each one run through state_path_findings() and confirmed MISSED before
# the fix. They are the same defect the line scan had, one layer up: the walk
# knew `/`, `+`, f-strings and `.format()`, so its limits list read as though
# string-assembled paths were covered, and three spellings were not.
#
# Scored against the predecessor line scan too (see the scoring test below): it
# misses all of these as well, so the "strictly stronger than what it replaced"
# claim still holds.
_DEFEATED_THE_FIRST_DRAFT_OF_THIS_WALK = {
    '%-formatting, the one formatting spelling that was missed': """
import os
base = os.path.dirname(os.path.dirname(__file__))
STATE_DIR = "%s/state" % base
os.makedirs(STATE_DIR, exist_ok=True)
""",
    '%-formatting with a mapping': """
import os
base = os.path.dirname(__file__)
STATE_DIR = "%(b)s/state" % {"b": base}
os.makedirs(STATE_DIR, exist_ok=True)
""",
    'os.sep.join over a literal sequence': """
import os
base = os.path.dirname(os.path.dirname(__file__))
STATE_DIR = os.sep.join([base, "state", "projects"])
os.makedirs(STATE_DIR, exist_ok=True)
""",
    'a literal slash join over a literal sequence': """
import os
base = os.path.dirname(__file__)
STATE_DIR = "/".join([base, "state", "projects"])
open(STATE_DIR + "/x.yaml", "w")
""",
    'segments accumulated by a loop rather than written out': """
from pathlib import Path
root = Path(__file__).parent.parent
for seg in ["state", "projects"]:
    root = root / seg
root.mkdir(parents=True, exist_ok=True)
""",
    'segments accumulated by a comprehension': """
from pathlib import Path
root = Path(__file__).parent
paths = [root / seg for seg in ["state", "projects"]]
for q in paths:
    q.mkdir(parents=True, exist_ok=True)
""",
}

_ALREADY_CORRECT = {
    'the resolver, joined and mkdir-ed': """
from config.state_manager import orchestrator_state_root
state_dir = orchestrator_state_root() / "projects" / project / "review_cycles"
state_dir.mkdir(parents=True, exist_ok=True)
(state_dir / "active_cycles.yaml").write_text(payload)
""",
    'the resolver behind a local name': """
from config.state_manager import orchestrator_state_root
root = orchestrator_state_root()
path = root / "execution_history" / f"{project}_issue_{number}.yaml"
path.write_text(payload)
""",
    'the word state in data, not in a path': """
payload = {"state": content.get("state")}
self.state = "running"
logger.info(f"state for {project} is {self.state}")
columns = ["state", "status"]
""",
    'a workspace path, which is not the state tree': """
from pathlib import Path
p = Path("/workspace") / project / "Dockerfile.agent"
p.write_text(dockerfile)
""",
    'a comma join that happens to contain the word state': """
header = ", ".join(["state", "status"])
for phase in ["state", "status"]:
    results[phase] = compute(phase)
""",
    'a %-formatted FILENAME under the resolver': """
from config.state_manager import orchestrator_state_root
p = orchestrator_state_root() / ("%s.yaml" % project)
p.write_text(payload)
""",
    'relative segments in a rule table (services/data_retention.py)': """
RULES = [
    RetentionRule(relative_path='state/execution_history', root_kind='orchestrator'),
    RetentionRule(relative_path='state/projects', root_kind='orchestrator'),
]
for rule in RULES:
    for entry in (resolve_roots()[rule.root_kind] / rule.relative_path).iterdir():
        pass
""",
}


class TestTheGuardItselfCatchesWhatDefeatedTheLastOne:
    """Mutation tests. The predecessor passed while #181 was live in five
    spellings, so this guard does not get to be trusted on its shape."""

    @pytest.mark.parametrize('description', sorted(_DEFEATED_THE_LINE_SCAN))
    def test_it_flags_every_shape_that_walked_past_the_line_scan(self, description):
        findings = state_path_findings(_DEFEATED_THE_LINE_SCAN[description], 'mutant.py')
        assert findings, f"not flagged: {description}"

    @pytest.mark.parametrize('description', sorted(_THE_LINE_SCAN_ALREADY_CAUGHT))
    def test_it_still_flags_what_the_line_scan_did_catch(self, description):
        findings = state_path_findings(_THE_LINE_SCAN_ALREADY_CAUGHT[description], 'mutant.py')
        assert findings, f"regression, the predecessor caught this: {description}"

    @pytest.mark.parametrize(
        'description', sorted(_DEFEATED_THE_FIRST_DRAFT_OF_THIS_WALK)
    )
    def test_it_flags_every_shape_that_walked_past_the_first_draft(self, description):
        """The second round of the same lesson. Each of these was MISSED by the
        walk as first written, and by its limits list, which named neither
        %-formatting nor a separator join nor a loop-accumulated segment."""
        findings = state_path_findings(
            _DEFEATED_THE_FIRST_DRAFT_OF_THIS_WALK[description], 'mutant.py'
        )
        assert findings, f"not flagged: {description}"

    @pytest.mark.parametrize('description', sorted(_ALREADY_CORRECT))
    def test_it_stays_quiet_on_code_that_is_already_right(self, description):
        findings = state_path_findings(_ALREADY_CORRECT[description], 'clean.py')
        assert findings == [], f"false positive: {description}\n  " + "\n  ".join(findings)

    def test_the_line_scan_it_replaced_scores_these_exactly_as_split_above(self):
        """The split between the two corpora above, re-measured here.

        These are the predecessor's two patterns, verbatim, scored the way it
        scored them: one line at a time, comments skipped. If someone moves a
        sample between the two dicts on a hunch, this fails. The first run of
        it moved three samples -- joinpath, os.path.join and a bare
        'state/projects' literal are all single-line and the old scan DID see
        them; the assumption that all eight defeated it was wrong.
        """
        derived_from_file = re.compile(r'__file__.*\bstate\b|\bstate\b.*__file__')
        literal_state_root = re.compile(
            r'["\'](?:/workspace/switchyard|/app)?/?state/'
            r'|(?:Path\(|os\.path\.join\()[^)]*["\']state["\']'
        )

        def old_scan_flags(source):
            for line in source.splitlines():
                if line.strip().startswith('#'):
                    continue
                if derived_from_file.search(line) or literal_state_root.search(line):
                    return True
            return False

        assert [d for d in sorted(_DEFEATED_THE_LINE_SCAN)
                if old_scan_flags(_DEFEATED_THE_LINE_SCAN[d])] == [], (
            "the old scan catches one of the samples filed under "
            "_DEFEATED_THE_LINE_SCAN"
        )
        assert [d for d in sorted(_THE_LINE_SCAN_ALREADY_CAUGHT)
                if not old_scan_flags(_THE_LINE_SCAN_ALREADY_CAUGHT[d])] == [], (
            "the old scan misses one of the samples filed under "
            "_THE_LINE_SCAN_ALREADY_CAUGHT"
        )
        assert [d for d in sorted(_DEFEATED_THE_FIRST_DRAFT_OF_THIS_WALK)
                if old_scan_flags(_DEFEATED_THE_FIRST_DRAFT_OF_THIS_WALK[d])] == [], (
            "a sample filed under _DEFEATED_THE_FIRST_DRAFT_OF_THIS_WALK is "
            "caught by the old scan, so it is not evidence that this walk is "
            "strictly stronger than what it replaced"
        )

    def test_a_name_bound_once_safely_and_once_not_is_still_flagged(self):
        """Branches are unioned. A guard that picked the safe binding would be
        defeated by an `if` -- which is how the fallback in five of the six
        files #203 fixed was written."""
        source = """
from pathlib import Path
from config.state_manager import orchestrator_state_root
if override:
    root = orchestrator_state_root()
else:
    root = Path(__file__).parent.parent / "state"
root.mkdir(parents=True, exist_ok=True)
"""
        findings = state_path_findings(source, 'mutant.py')
        assert findings and '__file__' in findings[0], findings

    def test_an_exempt_root_does_not_exempt_the_rest_of_its_file(self):
        """The narrowness that services/data_retention.py's whole-file entry
        did not have."""
        source = """
import os
from pathlib import Path
blessed = Path(os.environ.get('ORCHESTRATOR_ROOT', '/app')) / 'state' / 'locks'
sneaked = Path(__file__).parent.parent / 'state' / 'projects'
"""
        allowed = frozenset({"env:ORCHESTRATOR_ROOT->literal:'/app'"})
        findings = state_path_findings(source, 'mutant.py', allowed)
        assert len(findings) == 1 and '__file__' in findings[0], findings

        # An exemption whose file has stopped matching is a permanent blind
        # spot with no remaining justification, and the file it covers is
        # usually the one most worth watching -- config/state_manager.py sat
        # here, exempted, for exactly that reason after #202 moved the resolver
        # out from under it.
        assert set(EXEMPT) == exemptions_used, (
            "these EXEMPT entries no longer match anything, so they only "
            "hide whatever is added to those files next -- delete them: "
            f"{sorted(set(EXEMPT) - exemptions_used)}"
        )


class TestTheSuiteCannotReachTheDeploymentsState:

    def test_the_active_root_would_still_pass_the_import_time_guard(self):
        """The st_dev/st_ino identity check itself now lives in
        tests/conftest.py::_refuse_a_root_that_can_reach_the_deployment, where
        it runs at conftest import and RAISES (#202). Here it only ran partway
        through the session, so everything alphabetically before it had already
        written wherever the bad root pointed -- detection, not prevention, and
        22 tests including all 11 of this file's were observed green with
        ORCHESTRATOR_ROOT pointed at the checkout.

        What is left here is the end-state assertion: whatever the session is
        actually running with still satisfies the guard. That is not a
        tautology -- a test that reassigns os.environ['ORCHESTRATOR_ROOT']
        session-wide would break it, and conftest's guard would never see it.
        """
        from tests.conftest import _refuse_a_root_that_can_reach_the_deployment

        active = os.environ.get('ORCHESTRATOR_ROOT')
        assert active, "conftest must have redirected ORCHESTRATOR_ROOT"

        _refuse_a_root_that_can_reach_the_deployment(active, 'ORCHESTRATOR_ROOT')


class TestTheImportTimeGuardOnTheRoot:
    """tests/conftest.py::_refuse_a_root_that_can_reach_the_deployment.

    It used to be that `if os.environ.get('ORCHESTRATOR_ROOT'): return` --
    any preset value accepted as given, which is how the whole suite could be
    pointed at the live checkout and stay green (#202).
    """

    @staticmethod
    def _guard():
        from tests.conftest import _refuse_a_root_that_can_reach_the_deployment
        return _refuse_a_root_that_can_reach_the_deployment

    @staticmethod
    def _skip_without_a_deployment():
        if not Path('/app').is_dir():
            pytest.skip("no /app on this host; nothing to collide with")

    def test_an_absolute_scratch_root_is_accepted(self, tmp_path):
        assert self._guard()(str(tmp_path), 'ORCHESTRATOR_ROOT') == str(tmp_path)

    def test_a_relative_root_is_refused(self):
        with pytest.raises(RuntimeError, match='relative'):
            self._guard()('tmp/rv202', 'ORCHESTRATOR_ROOT')

    def test_the_deployment_directory_itself_is_refused(self):
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app', 'ORCHESTRATOR_ROOT')

    def test_an_alias_of_the_deployment_directory_is_refused(self):
        """Spelling is not identity. `/app/../app` normalises to the same inode
        and is exactly the shape `!= '/app'` used to wave through."""
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app/../app', 'ORCHESTRATOR_ROOT')

    def test_a_path_under_the_deployment_is_refused(self):
        """Stricter than the identity check this replaces, deliberately: the
        caller mkdir(parents=True)s an accepted root, so `/app/scratch` does
        not merely read production, it creates a directory inside it."""
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app/scratch-that-does-not-exist', 'ORCHESTRATOR_ROOT')

    def test_the_live_state_tree_is_refused(self):
        """The worst case, and the one #181 actually hit."""
        self._skip_without_a_deployment()

        with pytest.raises(RuntimeError, match='deployment'):
            self._guard()('/app/state', 'ORCHESTRATOR_ROOT')

    def test_the_message_names_the_variable_it_was_given(self):
        """Both callers share this function; an error naming the wrong
        environment variable sends the operator to the wrong place."""
        with pytest.raises(RuntimeError, match='SWITCHYARD_TEST_STATE_ROOT'):
            self._guard()('still-relative', 'SWITCHYARD_TEST_STATE_ROOT')

    def test_a_preset_root_goes_through_the_guard(self, monkeypatch):
        """The redirect honours a preset ORCHESTRATOR_ROOT -- the documented
        `docker exec -e ORCHESTRATOR_ROOT=/tmp/...` invocation depends on it --
        but no longer unvalidated."""
        self._skip_without_a_deployment()
        from tests.conftest import _redirect_orchestrator_root_to_scratch

        monkeypatch.setenv('ORCHESTRATOR_ROOT', '/app')

        with pytest.raises(RuntimeError, match='deployment'):
            _redirect_orchestrator_root_to_scratch()

    @pytest.mark.parametrize('preset', ['', '   '], ids=['empty', 'whitespace'])
    def test_a_blank_preset_is_redirected_to_scratch_not_read_as_unset(
        self, monkeypatch, preset
    ):
        """orchestrator_state_root() reads blank as unset and falls back to the
        checkout -- which is the deployment. conftest must NOT agree with it
        here: a blank preset has to become a scratch directory.

        The whitespace case is the one that was broken: `if
        os.environ.get('ORCHESTRATOR_ROOT'): return` saw `'   '` as truthy and
        returned, leaving it set, and every module then resolved the checkout.
        `''` was already falsy and fell through; it is parametrized alongside
        so the two cannot diverge again.
        """
        from tests.conftest import _redirect_orchestrator_root_to_scratch

        monkeypatch.setenv('ORCHESTRATOR_ROOT', preset)
        monkeypatch.delenv('SWITCHYARD_TEST_STATE_ROOT', raising=False)

        _redirect_orchestrator_root_to_scratch()

        chosen = os.environ['ORCHESTRATOR_ROOT']
        assert chosen.strip(), f"a blank preset ({preset!r}) was left in place"
        assert Path(chosen).is_dir()
        self._guard()(chosen, 'ORCHESTRATOR_ROOT')

    def test_a_relative_root_is_refused_rather_than_resolved_against_the_cwd(
        self, monkeypatch
    ):
        """`-e ORCHESTRATOR_ROOT=tmp/rv200`, a dropped leading slash, is the
        likeliest typo in the documented command -- and it used to mean "write
        under the checkout"."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', 'relative-oops')

        with pytest.raises(ValueError, match='absolute'):
            orchestrator_state_root()

    def test_whitespace_is_treated_as_unset(self, monkeypatch):
        """`-e ORCHESTRATOR_ROOT=` and a stray space are the same accident."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', '   ')
        import config.state_manager as sm

        assert orchestrator_state_root() == (
            Path(sm.__file__).parent.parent / 'state'
        ).resolve()


class TestTheReloadSentinel:
    """conftest's _restore_process_globals cannot undo an importlib.reload, so
    it detects one instead (#203). Both halves of that detection are measured
    here, because both can stop working silently."""

    def test_reload_replaces_the_spec_but_not_the_module_or_its_dict(self):
        """The premise the sentinel rests on, restated as a test.

        If a future CPython stops minting a fresh ModuleSpec on reload, the
        detector keeps passing and stops detecting -- a guard that fails
        silent, which is the specific failure this suite keeps paying for. The
        two negatives matter as much as the positive: module identity and
        `__dict__` identity both SURVIVE a reload, which is why
        `sys.modules.get(name) is module` cannot see one and why a sentinel
        attribute stamped into the module cannot either.

        config.retention is the module the suite actually reloads and the only
        entry on conftest's RELOADABLE_FIRST_PARTY_MODULES, so reloading it
        here is legal and costs nothing: no env var is patched, so it re-reads
        the same RETENTION_DAYS it already had.
        """
        import config.retention as retention

        before_module = sys.modules['config.retention']
        before_spec = retention.__spec__
        before_dict = retention.__dict__
        before_days = retention.RETENTION_DAYS
        probe = object()
        retention.__dict__['_switchyard_reload_probe'] = probe
        try:
            importlib.reload(retention)

            assert sys.modules['config.retention'] is before_module
            assert retention.__dict__ is before_dict
            assert retention.__dict__.get('_switchyard_reload_probe') is probe
            assert retention.__spec__ is not before_spec
            assert retention.RETENTION_DAYS == before_days
        finally:
            retention.__dict__.pop('_switchyard_reload_probe', None)

    def test_the_fixture_raises_on_an_unlisted_reload(self):
        """Drive _restore_process_globals directly over a synthetic module.

        In a real run this raise happens in teardown, so pytest records it as
        an ERROR against the test that reloaded, not a FAILURE -- the test body
        still passes. Verified end to end with a throwaway test that reloads
        services.review_cycle: `1 passed, 1 error`, the error naming the module.

        A synthetic one rather than a real reload: a real reload of a
        first-party module is the damage, and a test that does it to prove the
        detector works would be doing the thing the detector exists to stop.
        Swapping `__spec__` for a fresh object is precisely and only what
        reload does to the identity the fixture watches -- the test above is
        what ties that to a real reload.
        """
        conftest = sys.modules['tests.conftest']
        name = 'services._switchyard_reload_probe'
        module = types.ModuleType(name)
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        sys.modules[name] = module
        try:
            fixture = conftest._restore_process_globals.__wrapped__()
            next(fixture)
            module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)

            with pytest.raises(AssertionError, match='importlib.reload'):
                next(fixture, None)
        finally:
            sys.modules.pop(name, None)

    def test_an_allowlisted_module_is_not_failed(self):
        """The other direction: the allowlist has to actually exempt."""
        conftest = sys.modules['tests.conftest']
        name = sorted(conftest.RELOADABLE_FIRST_PARTY_MODULES)[0]
        assert name.startswith(conftest.FIRST_PARTY_PREFIXES), (
            f"{name} is allowlisted but does not match FIRST_PARTY_PREFIXES, so "
            f"the fixture never looked at it and the entry proves nothing"
        )
        module = sys.modules.get(name) or importlib.import_module(name)

        fixture = conftest._restore_process_globals.__wrapped__()
        next(fixture)
        before = module.__spec__
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        try:
            next(fixture, None)
        finally:
            module.__spec__ = before
