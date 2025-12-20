"""
Unit tests for status_calculator utilities.
"""

import pytest
from services.medic.shared.status_calculator import (
    calculate_severity,
    calculate_status,
    calculate_impact_score
)


class TestCalculateSeverity:
    """Tests for calculate_severity function."""

    def test_critical_level(self):
        """Test CRITICAL log level maps to CRITICAL severity."""
        log_entry = {"level": "CRITICAL"}
        assert calculate_severity(log_entry) == "CRITICAL"

    def test_fatal_level(self):
        """Test FATAL log level maps to CRITICAL severity."""
        log_entry = {"level": "FATAL"}
        assert calculate_severity(log_entry) == "CRITICAL"

    def test_error_level(self):
        """Test ERROR log level maps to ERROR severity."""
        log_entry = {"level": "ERROR"}
        assert calculate_severity(log_entry) == "ERROR"

    def test_warning_level(self):
        """Test WARNING log level maps to WARNING severity."""
        log_entry = {"level": "WARNING"}
        assert calculate_severity(log_entry) == "WARNING"

    def test_warn_level(self):
        """Test WARN log level maps to WARNING severity."""
        log_entry = {"level": "WARN"}
        assert calculate_severity(log_entry) == "WARNING"

    def test_unknown_level_defaults_to_error(self):
        """Test unknown log level defaults to ERROR."""
        log_entry = {"level": "INFO"}
        assert calculate_severity(log_entry) == "ERROR"

    def test_missing_level_defaults_to_error(self):
        """Test missing log level defaults to ERROR."""
        log_entry = {}
        assert calculate_severity(log_entry) == "ERROR"

    def test_case_insensitive(self):
        """Test that level matching is case insensitive."""
        log_entry = {"level": "error"}
        assert calculate_severity(log_entry) == "ERROR"


class TestCalculateStatus:
    """Tests for calculate_status function."""

    def test_new_status_first_occurrence(self):
        """Test new status for first occurrence."""
        status = calculate_status("new", 1, False)
        assert status == "new"

    def test_recurring_status_multiple_occurrences(self):
        """Test recurring status for multiple occurrences."""
        status = calculate_status("new", 2, False)
        assert status == "recurring"

    def test_trending_status_when_trending(self):
        """Test trending status when is_trending is True."""
        status = calculate_status("recurring", 5, True)
        assert status == "trending"

    def test_ignored_status_preserved(self):
        """Test that ignored status is not changed."""
        status = calculate_status("ignored", 10, True)
        assert status == "ignored"

    def test_resolved_status_preserved(self):
        """Test that resolved status is not changed."""
        status = calculate_status("resolved", 10, True)
        assert status == "resolved"

    def test_trending_overrides_recurring(self):
        """Test that trending status is set when trending even with high count."""
        status = calculate_status("recurring", 100, True)
        assert status == "trending"


class TestCalculateImpactScore:
    """Tests for calculate_impact_score function."""

    def test_zero_failures(self):
        """Test impact score with zero failures."""
        score = calculate_impact_score(0, 0)
        assert score == 0.0

    def test_single_failure_error_severity(self):
        """Test impact score with single failure at ERROR severity."""
        score = calculate_impact_score(1, 1, "ERROR")
        assert score == 1.0  # (1 * 0.7 + 1 * 0.3) * 1.0

    def test_critical_severity_multiplier(self):
        """Test that CRITICAL severity doubles the score."""
        score_error = calculate_impact_score(10, 10, "ERROR")
        score_critical = calculate_impact_score(10, 10, "CRITICAL")
        assert score_critical == score_error * 2

    def test_warning_severity_multiplier(self):
        """Test that WARNING severity halves the score."""
        score_error = calculate_impact_score(10, 10, "ERROR")
        score_warning = calculate_impact_score(10, 10, "WARNING")
        assert score_warning == score_error * 0.5

    def test_score_capped_at_100(self):
        """Test that score is capped at 100."""
        score = calculate_impact_score(1000, 1000, "CRITICAL")
        assert score == 100.0

    def test_failure_weighted_more_than_occurrence(self):
        """Test that failures are weighted 70% vs occurrences 30%."""
        score = calculate_impact_score(10, 0, "ERROR")
        assert score == 7.0  # 10 * 0.7

        score = calculate_impact_score(0, 10, "ERROR")
        assert score == 3.0  # 10 * 0.3

    def test_score_rounded_to_two_decimals(self):
        """Test that score is rounded to 2 decimal places."""
        score = calculate_impact_score(3, 3, "ERROR")
        assert isinstance(score, float)
        assert len(str(score).split('.')[-1]) <= 2
