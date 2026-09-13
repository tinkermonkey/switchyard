"""The shared source enumerator behind the four repo-wide guards (#221).

Four guards -- the unbounded-FileHandler scan, the #202 hand-rolled-root grep
and the two #181/#203 AST walks -- all ask the same question, "which files are
this checkout's", and all three files used to answer it with their own tuple of
directory names to skip. Those tuples had drifted (test_log_rotation.py's was
missing `.git`), and none of them could answer the question at all for a second
checkout parked under the root: a `.baseline-main/` worktree made every one of
the four fail, naming files nobody had written.

These tests pin the two properties that replaced the tuples, because both are
the kind that would go unnoticed if they quietly stopped holding: a nested
checkout is NOT scanned, and an untracked file IS.
"""

from pathlib import Path

from tests.utils.repo_sources import (
    NOT_FIRST_PARTY_TREES,
    REPO_ROOT,
    VENDORED_TREES,
    first_party_python_sources,
    is_nested_checkout,
)


def _tree(root, *relatives):
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')


def _relatives(root):
    return sorted(str(relative) for relative, _ in first_party_python_sources(root))


class TestNestedCheckoutsAreNotThisCheckout:

    def test_a_worktree_whose_git_is_a_FILE_is_not_descended_into(self, tmp_path):
        """`.baseline-main/` -- the shape that actually fired #221. `git
        worktree add` writes `.git` as a file containing `gitdir: ...`, so a
        test for a `.git` DIRECTORY would have missed the reported case
        entirely."""
        _tree(tmp_path, 'services/real.py', '.baseline-main/services/copy.py')
        (tmp_path / '.baseline-main' / '.git').write_text('gitdir: /elsewhere\n')

        assert _relatives(tmp_path) == ['services/real.py']

    def test_a_clone_whose_git_is_a_DIRECTORY_is_not_descended_into(self, tmp_path):
        _tree(tmp_path, 'services/real.py', 'vendored/services/copy.py')
        (tmp_path / 'vendored' / '.git' / 'objects').mkdir(parents=True)

        assert _relatives(tmp_path) == ['services/real.py']

    def test_it_prunes_at_any_depth_not_just_the_top_level(self, tmp_path):
        """The old skip lists only looked at `relative.parts[0]`, so a checkout
        one level down was invisible to them however the list was spelled."""
        _tree(tmp_path, 'services/real.py', 'scratch/copies/other/services/copy.py')
        (tmp_path / 'scratch' / 'copies' / 'other' / '.git').write_text('gitdir: /e\n')

        assert _relatives(tmp_path) == ['services/real.py']

    def test_the_root_itself_is_scanned_even_though_it_is_a_checkout(self, tmp_path):
        """The rule is about OTHER checkouts. The one being walked has a `.git`
        of its own and must not prune itself out of existence."""
        _tree(tmp_path, 'services/real.py')
        (tmp_path / '.git').mkdir()

        assert _relatives(tmp_path) == ['services/real.py']

    def test_is_nested_checkout_accepts_both_spellings_and_nothing_else(self, tmp_path):
        plain = tmp_path / 'plain'
        plain.mkdir()
        assert not is_nested_checkout(plain)

        worktree = tmp_path / 'worktree'
        worktree.mkdir()
        (worktree / '.git').write_text('gitdir: /elsewhere\n')
        assert is_nested_checkout(worktree)

        clone = tmp_path / 'clone'
        (clone / '.git').mkdir(parents=True)
        assert is_nested_checkout(clone)


class TestUntrackedFilesStayInScope:
    """The reason this is a walk and not `git ls-files '*.py'` (#221).

    A module written minutes ago and not yet `git add`ed is precisely where a
    fresh #181/#203 violation lives, and the moment at which saying so costs
    the author least. Measured before the fix: an untracked module dropped into
    services/ that constructs a FileHandler, reads ORCHESTRATOR_ROOT by hand,
    hangs a `state/` path off `Path(__file__)` and assigns
    `os.environ['ORCHESTRATOR_ROOT']` failed all four guards by name -- and was
    invisible to `git ls-files`. Asking git would have narrowed every one of
    them while looking like a simplification.
    """

    def test_a_file_git_has_never_seen_is_enumerated(self, tmp_path):
        _tree(tmp_path, 'services/tracked.py', 'services/brand_new.py')
        (tmp_path / '.git').mkdir()

        assert _relatives(tmp_path) == [
            'services/brand_new.py', 'services/tracked.py'
        ]


class TestTheNameListsThatRemain:

    def test_this_checkouts_non_source_trees_are_skipped_at_the_top_level(self, tmp_path):
        _tree(tmp_path, 'services/real.py',
              *[f'{name}/inside.py' for name in NOT_FIRST_PARTY_TREES])

        assert _relatives(tmp_path) == ['services/real.py']

    def test_those_names_are_skipped_ONLY_at_the_top_level(self, tmp_path):
        """`services/state/manager.py` is source; `state/` at the root is the
        gitignored runtime tree. Matching the name at any depth would have made
        the list mean something the four guards never meant by it."""
        _tree(tmp_path, 'services/state/manager.py', 'state/runtime.py')

        assert _relatives(tmp_path) == ['services/state/manager.py']

    def test_dependency_trees_are_skipped_at_ANY_depth(self, tmp_path):
        """The one real difference this makes to the live checkout, and the one
        thing the old `parts[0] in (...)` test could not express:
        `web_ui/node_modules/...` is vendored third-party code sitting below
        the top level, and all three old tuples scanned it as first-party."""
        _tree(tmp_path, 'services/real.py',
              *[f'web_ui/{name}/vendored.py' for name in VENDORED_TREES])

        assert _relatives(tmp_path) == ['services/real.py']


class TestItAgreesWithTheRealCheckout:

    def test_every_path_it_yields_exists_and_is_python(self):
        found = first_party_python_sources()

        assert found, 'the enumerator found no source in this checkout at all'
        for relative, absolute in found:
            assert absolute.suffix == '.py'
            assert absolute.is_file()
            assert absolute == REPO_ROOT / relative

    def test_it_finds_the_modules_the_guards_exist_for(self):
        """A scanner that silently enumerates nothing passes every guard built
        on it. These four are the files the #181/#202/#203 work is about."""
        relatives = {str(relative) for relative, _ in first_party_python_sources()}

        for expected in ('main.py', 'config/paths.py', 'config/state_manager.py',
                         'services/data_retention.py'):
            assert expected in relatives, expected

    def test_it_yields_no_test_module_and_no_nested_worktree(self):
        relatives = [str(relative) for relative, _ in first_party_python_sources()]

        assert not [r for r in relatives if r.startswith('tests/')]
        assert not [r for r in relatives if r.startswith('.claude/')]
        assert relatives == sorted(relatives)
        assert len(relatives) == len(set(relatives))
