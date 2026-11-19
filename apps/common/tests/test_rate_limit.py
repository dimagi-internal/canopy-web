from unittest import mock

from django.core.cache import cache

from apps.common import rate_limit
from apps.common.rate_limit import over


def setup_function():
    cache.clear()


def test_refuses_past_the_limit_within_a_window():
    with mock.patch.object(rate_limit.time, "time", return_value=1000.0):
        assert [over("k", 2, 60) for _ in range(3)] == [False, False, True]


def test_a_new_window_starts_fresh_even_if_the_old_counter_never_expires():
    with mock.patch.object(rate_limit.time, "time", return_value=1000.0):
        for _ in range(5):
            over("k", 2, 60)
        assert over("k", 2, 60) is True
        slot = f"k:{int(1000.0 // 60)}"
        cache.set(slot, 10_000, timeout=None)   # a counter that lost its expiry
    with mock.patch.object(rate_limit.time, "time", return_value=1000.0 + 60):
        assert over("k", 2, 60) is False


def test_a_counter_incr_recreated_gets_its_expiry_back():
    with mock.patch.object(rate_limit.time, "time", return_value=1000.0), \
            mock.patch.object(rate_limit.cache, "add", return_value=False), \
            mock.patch.object(rate_limit.cache, "incr", return_value=1), \
            mock.patch.object(rate_limit.cache, "touch") as touch:
        assert over("k", 2, 60) is False
    touch.assert_called_once_with(f"k:{int(1000.0 // 60)}", 120)
