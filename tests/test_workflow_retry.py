"""Retry policy: backoff curve, attempt bound, injected transient classification."""
from __future__ import annotations

import pytest

from workflow.retry import RetryPolicy, default_backoff


class TestBackoffCurve:
    @pytest.mark.parametrize("attempt,expected", [
        (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0), (6, 30.0), (99, 30.0),
    ])
    def test_exponential_to_the_ceiling(self, attempt, expected):
        assert default_backoff(attempt) == expected

    def test_attempt_zero_or_negative_clamps_to_one_second(self):
        assert default_backoff(0) == 1.0
        assert default_backoff(-3) == 1.0

    def test_curve_shape_overridable(self):
        assert default_backoff(3, factor=3.0, cap=6.0) == 6.0


class TestRetryDecision:
    @staticmethod
    def _boom_is_transient(exc: BaseException) -> bool:
        return isinstance(exc, TimeoutError)

    def test_default_policy_never_retries(self):
        assert RetryPolicy().should_retry(TimeoutError(), attempt=1) is False

    def test_transient_below_bound_retries(self):
        p = RetryPolicy(max_attempts=3, is_transient=self._boom_is_transient)
        assert p.should_retry(TimeoutError(), attempt=1) is True
        assert p.should_retry(TimeoutError(), attempt=2) is True

    def test_final_attempt_does_not_retry(self):
        p = RetryPolicy(max_attempts=3, is_transient=self._boom_is_transient)
        assert p.should_retry(TimeoutError(), attempt=3) is False

    def test_non_transient_never_retries(self):
        p = RetryPolicy(max_attempts=5, is_transient=self._boom_is_transient)
        assert p.should_retry(ValueError("nope"), attempt=1) is False

    def test_classification_is_fully_injected(self):
        # A policy that calls EVERYTHING transient (e.g. a permissive deploy) is legal here:
        # the core owns the shape, the adapter owns the vocabulary.
        p = RetryPolicy(max_attempts=2, is_transient=lambda exc: True)
        assert p.should_retry(ValueError(), attempt=1) is True
        # And a lost-lease style error can be classified non-transient at the adapter.
        assert RetryPolicy(max_attempts=2).should_retry(ValueError(), attempt=1) is False

    def test_wait_and_audit_view(self):
        p = RetryPolicy(max_attempts=3, is_transient=self._boom_is_transient)
        assert p.wait_s(3) == 4.0
        view = p.classified(TimeoutError("t"))
        assert view["transient"] is True
        assert view["max_attempts"] == 3
        assert view["error"] == "TimeoutError: t"
