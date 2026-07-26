"""Per-app-credential rate limiting for token-exchange (F2, 2026-07-26
security review). Unit tests for the counter; the 429 wiring is covered in
tests/test_token_exchange.py."""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.tokens.rate_limit import ExchangeRateLimitError, check_exchange_limit


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@override_settings(TOKEN_EXCHANGE_LIMIT=3, TOKEN_EXCHANGE_WINDOW_SECONDS=60)
def test_allows_up_to_limit():
    for _ in range(3):
        check_exchange_limit(1)  # no raise


@override_settings(TOKEN_EXCHANGE_LIMIT=3, TOKEN_EXCHANGE_WINDOW_SECONDS=60)
def test_raises_over_limit():
    for _ in range(3):
        check_exchange_limit(1)
    with pytest.raises(ExchangeRateLimitError):
        check_exchange_limit(1)


@override_settings(TOKEN_EXCHANGE_LIMIT=2, TOKEN_EXCHANGE_WINDOW_SECONDS=60)
def test_limits_are_per_credential():
    check_exchange_limit(1)
    check_exchange_limit(1)
    # A different credential has its own budget.
    check_exchange_limit(2)
    check_exchange_limit(2)
    with pytest.raises(ExchangeRateLimitError):
        check_exchange_limit(1)
