"""The debug knob for shortening a lifetime can only shorten, never lengthen."""
import pytest

from apps.tokens.debug_lifetime import debug_shortened

NAME = "CANOPY_TEST_LIFETIME_SECONDS"


def test_unset_keeps_the_default(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    assert debug_shortened(1800, NAME) == 1800


@pytest.mark.parametrize("asked,expected", [("30", 30), ("1", 5), ("0", 5), ("-9", 5)])
def test_a_shorter_value_wins_down_to_five_seconds(monkeypatch, asked, expected):
    monkeypatch.setenv(NAME, asked)
    assert debug_shortened(1800, NAME) == expected


@pytest.mark.parametrize("asked", ["1800", "3600", "86400"])
def test_it_never_lengthens(monkeypatch, asked):
    monkeypatch.setenv(NAME, asked)
    assert debug_shortened(1800, NAME) == 1800


@pytest.mark.parametrize("asked", ["soon", "1.5", " "])
def test_a_non_number_is_ignored(monkeypatch, asked):
    monkeypatch.setenv(NAME, asked)
    assert debug_shortened(1800, NAME) == 1800
