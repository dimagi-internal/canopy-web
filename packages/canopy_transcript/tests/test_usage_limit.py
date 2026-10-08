import datetime as dt

from canopy_transcript.usage_limit import is_usage_limit, limit_in_record, reset_at

UTC = dt.timezone.utc


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_the_real_session_cap_resets_later_the_same_night():
    # Observed 2026-10-08T07:38:01Z on this fleet — 01:38 in Denver.
    text = "You've hit your session limit · resets 2:30am (America/Denver)"
    assert is_usage_limit(text)
    assert reset_at(text, at("2026-10-08T07:38:01")) == at("2026-10-08T08:30:00")


def test_a_wall_clock_already_past_today_means_tomorrow():
    text = "You've hit your limit · resets 1am (America/Denver)"
    assert reset_at(text, at("2026-10-08T07:38:01")) == at("2026-10-09T07:00:00")


def test_a_dated_weekly_reset():
    text = "You've hit your weekly limit · resets Aug 3, 11pm (UTC)"
    assert reset_at(text, at("2026-08-01T12:00:00")) == at("2026-08-03T23:00:00")


def test_pm_and_noon_and_midnight():
    now = at("2026-10-08T00:30:00")
    assert reset_at("resets 3pm (UTC)", now) == at("2026-10-08T15:00:00")
    assert reset_at("resets 12pm (UTC)", now) == at("2026-10-08T12:00:00")
    assert reset_at("resets 12am (UTC)", now) == at("2026-10-09T00:00:00")


def test_the_legacy_epoch_form():
    now = at("2026-10-08T07:00:00")
    epoch = int(at("2026-10-08T09:00:00").timestamp())
    assert reset_at(f"Claude AI usage limit reached|{epoch}", now) == at("2026-10-08T09:00:00")


def test_relative_form():
    assert reset_at("limit reached, resets in 2h 15m", at("2026-10-08T07:00:00")) == \
        at("2026-10-08T09:15:00")


def test_unreadable_or_absurd_resets_are_none_not_a_guess():
    now = at("2026-10-08T07:00:00")
    assert reset_at("You've hit your limit", now) is None
    assert reset_at("resets 13pm (UTC)", now) is None
    assert reset_at("resets 3pm (Not/AZone)", now) is not None  # falls back to local
    past = int(at("2026-10-08T06:00:00").timestamp())
    assert reset_at(f"usage limit reached|{past}", now) is None
    far = int(at("2026-11-08T06:00:00").timestamp())
    assert reset_at(f"usage limit reached|{far}", now) is None


def test_ordinary_text_is_not_a_cap():
    assert not is_usage_limit("I reset the counter and checked the rate limiter")
    assert not is_usage_limit("")


def _cap_record(**over):
    rec = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
           "message": {"model": "<synthetic>", "content": [
               {"type": "text", "text": "You've hit your session limit · resets 2:30am (America/Denver)"}]}}
    rec.update(over)
    return rec


def test_the_real_cap_record_is_recognised():
    assert limit_in_record(_cap_record()).startswith("You've hit your session limit")


def test_an_agent_writing_about_limits_is_not_a_cap():
    # The text alone must never park a box — only Claude Code's own error record.
    assert limit_in_record(_cap_record(isApiErrorMessage=False)) is None
    assert limit_in_record({"type": "user", "message": {"content": "you've hit your limit"}}) is None


def test_an_api_error_that_is_not_a_cap_is_ignored():
    rec = _cap_record(error="server_error")
    rec["message"]["content"] = [{"type": "text", "text": "API Error: 500 Internal server error"}]
    assert limit_in_record(rec) is None
