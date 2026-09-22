"""The counter behind `POST /api/embed/token`'s per-user mint budget.

This file used to test the per-credential budget for `/api/auth/token-exchange`,
which went with that endpoint (2026-09-22). The mint limiter it sat beside had
no unit tests of its own, so the tests moved rather than the coverage going to
zero: it is the same fixed-window counter, and the property that matters is
that one user's budget is not spent by another's traffic.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.tokens.rate_limit import MintRateLimitError, check_mint_limit


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@override_settings(EMBED_MINT_LIMIT=3, EMBED_MINT_WINDOW_SECONDS=60)
def test_allows_up_to_limit():
    for _ in range(3):
        check_mint_limit(1)  # no raise


@override_settings(EMBED_MINT_LIMIT=1, EMBED_MINT_WINDOW_SECONDS=60)
def test_raises_over_limit():
    check_mint_limit(1)
    with pytest.raises(MintRateLimitError):
        check_mint_limit(1)


@override_settings(EMBED_MINT_LIMIT=2, EMBED_MINT_WINDOW_SECONDS=60)
def test_limits_are_per_user():
    """One person looping must not lock everybody else out of their own chat."""
    check_mint_limit(1)
    check_mint_limit(1)
    # A different user still has their whole budget.
    check_mint_limit(2)
    check_mint_limit(2)
    with pytest.raises(MintRateLimitError):
        check_mint_limit(1)
