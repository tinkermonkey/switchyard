"""
Reporting of container-gated test files (#140 item 37).

~60 test files call pytest.skip("Requires Docker container environment",
allow_module_level=True) when /app is absent, because they import
agents/__init__.py and other modules that genuinely cannot import outside the
orchestrator container (CLAUDE.md, "Docker-only imports"). The gate is correct;
what was wrong is that it was silent -- pytest's summary reports those skips
indistinguishably from any other, so a host run of the suite looked green while
a large share of it never executed, with no CI to notice (#140 item 36 -- no
workflow was added: the suite does not currently run to completion on a bare
runner, it blocks on connects to the hardcoded redis/elasticsearch hostnames,
so a workflow would hang to its job timeout. Measured and filed as #174).

Converting all ~60 files to degrade to mocks was considered and rejected: it is
a large refactor of tests written to exercise real imports, with a real risk of
weakening the assertions those files exist for. Making the gap loud is the part
that carries signal now. These tests drive the hooks directly, because the
condition they report on (no /app) is by construction not the environment the
suite runs in.
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import (
    CONTAINER_ONLY_SKIP_REASON,
    ORCHESTRATOR_CONTAINER_MARKER,
    pytest_report_header,
    pytest_terminal_summary,
)


def _skip_report(reason):
    report = MagicMock()
    report.longrepr = ('tests/unit/test_something.py', 12, f'Skipped: {reason}')
    return report


def _reporter(skipped):
    reporter = MagicMock()
    reporter.stats = {'skipped': list(skipped)}
    reporter.lines = []
    reporter.write_line.side_effect = lambda line, **kw: reporter.lines.append(line)
    reporter.write_sep.side_effect = lambda sep, title='', **kw: reporter.lines.append(title)
    return reporter


@pytest.fixture
def outside_container():
    with patch('tests.conftest.os.path.isdir', return_value=False):
        yield


@pytest.fixture
def inside_container():
    with patch('tests.conftest.os.path.isdir', return_value=True):
        yield


class TestHeader:

    def test_names_the_container_when_inside_it(self, inside_container):
        assert 'yes' in pytest_report_header(MagicMock())

    def test_warns_loudly_when_outside_it(self, outside_container):
        header = pytest_report_header(MagicMock())
        assert 'NO' in header
        assert 'SKIPPED' in header
        # The header is where someone learns how to get the missing coverage.
        assert 'docker exec' in header

    def test_names_the_marker_it_actually_checks(self, outside_container):
        assert ORCHESTRATOR_CONTAINER_MARKER in pytest_report_header(MagicMock())


class TestTerminalSummary:

    def test_reports_the_number_of_files_that_never_ran(self, outside_container):
        reporter = _reporter([
            _skip_report(CONTAINER_ONLY_SKIP_REASON),
            _skip_report(CONTAINER_ONLY_SKIP_REASON),
        ])

        pytest_terminal_summary(reporter, 0, MagicMock())

        assert any('2 container-gated test file(s) did NOT run' in line
                   for line in reporter.lines), reporter.lines

    def test_says_the_result_does_not_cover_them(self, outside_container):
        """The point is that a green line must not be read as a full pass."""
        reporter = _reporter([_skip_report(CONTAINER_ONLY_SKIP_REASON)])

        pytest_terminal_summary(reporter, 0, MagicMock())

        assert any('does not cover them' in line for line in reporter.lines), reporter.lines

    def test_ignores_unrelated_skips(self, outside_container):
        """A test skipped for its own reasons is not a coverage gap in the
        container sense, and counting it would make the number meaningless."""
        reporter = _reporter([_skip_report('needs a live GitHub token')])

        pytest_terminal_summary(reporter, 0, MagicMock())

        assert reporter.lines == []

    def test_says_nothing_when_there_are_no_gated_skips(self, outside_container):
        reporter = _reporter([])

        pytest_terminal_summary(reporter, 0, MagicMock())

        assert reporter.lines == []

    def test_says_nothing_inside_the_container(self, inside_container):
        """Inside the container the gated files ran, so there is no gap to
        report -- and a banner on every containerized run would be noise."""
        reporter = _reporter([_skip_report(CONTAINER_ONLY_SKIP_REASON)])

        pytest_terminal_summary(reporter, 0, MagicMock())

        assert reporter.lines == []


class TestTheGateReasonStringMatchesTheTests:

    def test_the_reason_string_is_the_one_test_files_actually_pass(self):
        """
        The count is matched on this exact string, so a file using a different
        wording would be missed. Checked against the real test tree rather than
        asserted from memory.
        """
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ['grep', '-rl', 'allow_module_level=True', 'tests/'],
            cwd=repo_root, capture_output=True, text=True,
        )
        gated_files = [line for line in result.stdout.splitlines() if line]
        assert gated_files, "expected the repo to still have container-gated test files"

        mismatched = []
        for relative_path in gated_files:
            text = (repo_root / relative_path).read_text()
            if 'allow_module_level=True' in text and CONTAINER_ONLY_SKIP_REASON not in text:
                mismatched.append(relative_path)

        assert not mismatched, (
            "these files gate at module level with a reason the summary hook does not "
            f"recognise, so their skips would go uncounted: {mismatched}"
        )
