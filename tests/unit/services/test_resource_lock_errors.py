"""
Unit tests for services/resource_lock_errors.py — the single choke point that
decides whether an exception is a project-scoped resource lock timeout.

Three layers depend on this answer being right (#148, covering #140 items
8/10/13/25): agent_executor.py's retry loop (never retry contention),
circuit_breaker.py (never count contention against an agent+project breaker),
and project_workspace.py's startup init (report UNKNOWN, never "confirmed no
setup needed"). Its two interesting properties are both tested here: it sees
through an explicit `raise ... from <lock timeout>` re-wrap, and it does NOT
see through implicit __context__ chaining.
"""

import pytest
from unittest.mock import patch

from services.resource_lock_errors import (
    describe_lock_timeout,
    is_lock_timeout_error,
    lock_timeout_error_types,
)
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


class TestDirectRecognition:
    def test_both_lock_timeout_types_are_recognised(self):
        assert is_lock_timeout_error(ProjectCheckoutLockTimeoutError("busy"))
        assert is_lock_timeout_error(DevContainerBuildLockTimeoutError("busy"))

    def test_types_tuple_contains_exactly_the_two_lock_timeouts(self):
        assert set(lock_timeout_error_types()) == {
            ProjectCheckoutLockTimeoutError,
            DevContainerBuildLockTimeoutError,
        }

    def test_ordinary_exceptions_are_not_recognised(self):
        assert not is_lock_timeout_error(RuntimeError("agent blew up"))
        assert not is_lock_timeout_error(Exception("agent blew up"))
        assert not is_lock_timeout_error(ValueError("bad config"))

    def test_none_is_not_recognised(self):
        assert not is_lock_timeout_error(None)

    def test_unloadable_lock_types_degrade_instead_of_masking_the_real_error(self):
        """Every caller is inside an except block handling somebody else's
        exception — an import failure here must not become the exception that
        propagates in its place."""
        with patch(
            "services.resource_lock_errors.lock_timeout_error_types", return_value=()
        ):
            assert not is_lock_timeout_error(ProjectCheckoutLockTimeoutError("busy"))
            assert describe_lock_timeout(RuntimeError("boom")) == "RuntimeError: boom"


class TestCauseChain:
    """Agents re-wrap whatever run_claude_code() raises into a plain
    Exception(...) from exc. Recognition has to survive that."""

    def test_explicitly_chained_lock_timeout_is_recognised(self):
        inner = ProjectCheckoutLockTimeoutError("busy")
        try:
            try:
                raise inner
            except Exception as exc:
                raise Exception(f"Agent execution failed: {exc}") from exc
        except Exception as wrapper:
            assert is_lock_timeout_error(wrapper)

    def test_recognised_through_two_levels_of_wrapping(self):
        inner = DevContainerBuildLockTimeoutError("busy")
        try:
            try:
                try:
                    raise inner
                except Exception as exc:
                    raise Exception("inner wrapper") from exc
            except Exception as exc:
                raise RuntimeError("outer wrapper") from exc
        except Exception as wrapper:
            assert is_lock_timeout_error(wrapper)

    def test_implicit_context_chaining_is_NOT_followed(self):
        """`raise X from exc` says X *is* exc re-expressed. A bare `raise X`
        inside an except block sets only __context__, which any unrelated
        secondary failure gets for free — following it would misclassify a
        genuine agent failure that merely happened while a lock timeout was
        being handled."""
        try:
            try:
                raise ProjectCheckoutLockTimeoutError("busy")
            except Exception:
                raise RuntimeError("a genuinely different failure")
        except Exception as secondary:
            assert secondary.__context__ is not None
            assert secondary.__cause__ is None
            assert not is_lock_timeout_error(secondary)

    def test_chain_of_non_lock_errors_is_not_recognised(self):
        try:
            try:
                raise ValueError("root")
            except Exception as exc:
                raise Exception("wrapper") from exc
        except Exception as wrapper:
            assert not is_lock_timeout_error(wrapper)

    def test_self_referencing_cause_terminates(self):
        """Guard against a pathological chain spinning the walk forever."""
        exc = RuntimeError("loop")
        exc.__cause__ = exc
        assert not is_lock_timeout_error(exc)


class TestDescribeLockTimeout:
    def test_names_the_inner_lock_timeout_not_the_wrapper(self):
        try:
            try:
                raise ProjectCheckoutLockTimeoutError("held by another operation")
            except Exception as exc:
                raise Exception(f"Business Analyst execution failed: {exc}") from exc
        except Exception as wrapper:
            described = describe_lock_timeout(wrapper)

        assert described.startswith("ProjectCheckoutLockTimeoutError:")
        assert "held by another operation" in described

    def test_falls_back_to_the_exception_itself_when_no_lock_timeout_present(self):
        described = describe_lock_timeout(RuntimeError("something else"))
        assert described == "RuntimeError: something else"
