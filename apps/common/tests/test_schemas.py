import datetime as dt

from apps.common.schemas import (
    HealthOut,
    MeOut,
    StrictModel,
    TimestampMixin,
    UserRefOut,
)


def test_user_ref_round_trip():
    raw = {"id": 42, "email": "alice@dimagi.com", "display_name": "Alice"}
    parsed = UserRefOut.model_validate(raw)
    assert parsed.email == "alice@dimagi.com"
    dumped = parsed.model_dump()
    assert dumped == raw


def test_user_ref_display_name_optional():
    parsed = UserRefOut.model_validate({"id": 1, "email": "a@b.com"})
    assert parsed.display_name is None


def test_timestamp_mixin_iso8601():
    when = dt.datetime(2026, 5, 26, 12, 0, tzinfo=dt.UTC)

    class _S(TimestampMixin):
        pass

    s = _S(created_at=when, updated_at=when)
    dumped = s.model_dump(mode="json")
    assert dumped["created_at"].endswith("Z") or "+00:00" in dumped["created_at"]


def test_strict_model_rejects_extra_fields():
    import pytest
    from pydantic import ValidationError

    class _S(StrictModel):
        a: int

    with pytest.raises(ValidationError):
        _S.model_validate({"a": 1, "rogue": 2})


def test_health_out():
    parsed = HealthOut.model_validate({"status": "ok"})
    assert parsed.status == "ok"


def test_me_out_round_trip():
    parsed = MeOut.model_validate({
        "email": "alice@dimagi.com",
        "name": "Alice",
        "avatar_url": "https://x.com/y.png",
    })
    assert parsed.email == "alice@dimagi.com"
