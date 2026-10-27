"""A box behind the canopy release is asked to refresh, on its next heartbeat.

cloud-ec2-2 sat on canopy 0.2.804 for hours on 2026-10-27 after 0.2.805 stopped
an hourly owner alert: a cloud box re-runs bootstrap (which updates the plugin and
the CLI) only on a runner-code deploy, an operator's Refresh, or 24h of age. The
doorbell keyed on the runner's code sha alone, so a canopy-only release rang
nothing. canopy-web now knows the release (`canopy_release.expected`) and turns
"behind it" into the heartbeat reply's `refresh_pending` — the box's existing,
idle-gated, floor-limited self-refresh.
"""
from __future__ import annotations

import time
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.harness import canopy_release, runners, services
from apps.harness.models import Runner

pytestmark = pytest.mark.django_db


class _Resp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def release(monkeypatch):
    """Set canopy's released version ("" = GitHub unreadable)."""
    def _set(version: str):
        cache.delete(canopy_release.CACHE_KEY)
        resp = _Resp(200, {"name": "canopy", "version": version}) if version else _Resp(404)
        monkeypatch.setattr(canopy_release, "_raw_get", lambda url: resp)
    return _set


def _health(cli: str, *, booted: float = 1.0, explicit: bool = True) -> dict:
    health = {"checks": [{"name": "canopy.cli", "status": "ok", "detail": f"canopy {cli}"}],
              "checked_at": time.time(), "bootstrapped_at": booted}
    if explicit:
        health["canopy_version"] = cli
    return health


@pytest.fixture
def box(django_user_model):
    u = django_user_model.objects.create_user(username="jj", email="jj@dimagi.com", password="x")
    r = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=u,
                              status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    c = Client()
    c.force_login(u)
    return r, c


def _beat(c, r, **body):
    return c.post(f"/api/harness/runners/{r.id}/heartbeat",
                  data={"active_turn_ids": [], **body}, content_type="application/json").json()


# --- the release -------------------------------------------------------------

def test_the_release_is_read_once_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(canopy_release, "_raw_get",
                        lambda url: calls.append(url) or _Resp(200, {"version": "0.2.805"}))
    assert canopy_release.expected() == "0.2.805"
    assert canopy_release.expected() == "0.2.805"
    assert calls == [canopy_release.RAW_URL]


@pytest.mark.parametrize("resp", [_Resp(404), _Resp(200, ValueError("not json")),
                                  _Resp(200, {"version": "main"}), _Resp(200, ["0.2.805"])])
def test_an_unreadable_release_is_unknown(monkeypatch, resp):
    monkeypatch.setattr(canopy_release, "_raw_get", lambda url: resp)
    assert canopy_release.expected() == ""


@pytest.mark.parametrize("have,want,is_behind", [
    ("0.2.804", "0.2.805", True),
    ("0.2.99", "0.2.805", True),     # numeric, not string, order
    ("0.2.805", "0.2.805", False),
    ("0.2.900", "0.2.805", False),   # ahead: a developer's install
    ("", "0.2.805", False),
    ("0.2.804", "", False),
])
def test_behind_is_strictly_lower_and_unknown_is_never_behind(have, want, is_behind):
    assert canopy_release.behind(have, want) is is_behind


def test_an_older_box_is_read_from_its_check_detail():
    assert canopy_release.reported(_health("0.2.804", explicit=False)) == "0.2.804"
    stale = {"checks": [{"name": "canopy.cli", "status": "warn",
                         "detail": "canopy CLI 0.2.804, but the plugin is 0.2.805: refresh this runner"}]}
    assert canopy_release.reported(stale) == "0.2.804"
    assert canopy_release.reported({"checks": [{"name": "canopy.cli", "status": "fail",
                                                "detail": "the canopy CLI did not run"}]}) == ""


# --- the heartbeat -----------------------------------------------------------

def test_a_box_behind_the_release_is_asked_to_refresh(box, release):
    r, c = box
    release("0.2.805")
    out = _beat(c, r, health=_health("0.2.804"))
    assert out["refresh_pending"] is True
    assert out["expected_canopy_version"] == "0.2.805"


def test_the_explicit_version_is_stored_and_read(box, release):
    r, c = box
    release("0.2.805")
    health = {"checks": [], "bootstrapped_at": 1.0, "canopy_version": "0.2.804"}
    assert _beat(c, r, health=health)["refresh_pending"] is True
    r.refresh_from_db()
    assert r.health["canopy_version"] == "0.2.804"


@pytest.mark.parametrize("cli,rel", [("0.2.805", "0.2.805"), ("0.2.900", "0.2.805"),
                                     ("0.2.804", ""), ("", "0.2.805")])
def test_equal_ahead_or_unknown_asks_nothing(box, release, cli, rel):
    r, c = box
    release(rel)
    health = _health(cli) if cli else {"checks": [], "bootstrapped_at": 1.0}
    assert _beat(c, r, health=health)["refresh_pending"] is False


def test_a_bootstrap_after_the_release_discharges_the_ask(box, release):
    # A box that re-bootstrapped and is STILL behind could not get there; asking
    # again would only restart it every half hour. Its canopy.cli check says so.
    r, c = box
    release("0.2.805")
    assert _beat(c, r, health=_health("0.2.804", booted=1.0))["refresh_pending"] is True
    assert _beat(c, r, health=_health("0.2.804", booted=time.time() + 1))["refresh_pending"] is False


def test_an_operator_refresh_still_works_alone(box, release):
    r, c = box
    release("0.2.805")
    _beat(c, r, health=_health("0.2.805", booted=1.0))
    r.refresh_from_db()
    runners.request_refresh(r)
    assert _beat(c, r)["refresh_pending"] is True


def test_the_runner_code_doorbell_is_unchanged(box, release, settings):
    r, _ = box
    settings.RUNNER_CLOUD_CODE_SHA = "2" * 40
    release("0.2.805")
    with mock.patch("apps.realtime.groups.publish") as pub:
        services.heartbeat(r, active_turn_ids=[], code_sha="2" * 40, health=_health("0.2.804"))
    assert not any(call.args[1].get("type") == "runner.update_available" for call in pub.call_args_list)
    with mock.patch("apps.realtime.groups.publish") as pub:
        services.heartbeat(r, active_turn_ids=[], code_sha="9" * 40)
    assert [call.args[1] for call in pub.call_args_list
            if call.args[1].get("type") == "runner.update_available"] == [
        {"type": "runner.update_available", "expected_sha": "2" * 40}]
