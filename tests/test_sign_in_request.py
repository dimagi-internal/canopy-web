"""A runner pushes an AWS device-code approval link to its owner's phone.

The parts that are easy to get wrong without anything failing loudly: who the
push reaches (only the owner; an approved device code grants AWS access to
whoever started it), what it may open (only an AWS device page, because the
service worker opens it without the person first seeing it), and that 0 sends
reads as 0 so the box knows to fall back. And, since a push the push service
accepted is not one the phone showed: the request waits on the owner's
supervisor feed, to them alone, only for as long as its code lives.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.harness.models import Runner, SignInRequest
from apps.push import services as push_services

pytestmark = pytest.mark.django_db

AWS_URL = "https://commcare-connect.awsapps.com/start/#/device?user_code=ABCD-EFGH"


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def runner(owner):
    return Runner.objects.create(name="jj-mbp", kind=Runner.CLOUD, owner=owner)


@pytest.fixture()
def sent(monkeypatch):
    calls: list[dict] = []

    def fake(user, **kw):
        calls.append({"user": user.username, **kw})
        return 1

    monkeypatch.setattr(push_services, "send_to_user", fake)
    return calls


def _post(user, runner, body):
    c = Client()
    c.force_login(user)
    body = {"reason": "read the canopy-web 5xx logs", **body}
    return c.post(f"/api/harness/runners/{runner.id}/sign-in-request",
                  data=json.dumps(body), content_type="application/json")


def test_the_owner_is_pushed_a_tap_to_approve_link(owner, runner, sent):
    r = _post(owner, runner, {"url": AWS_URL, "label": "labs", "requested_by": "hal",
                              "reason": "confirm the 5xx alarm cause in /ecs/labs-jj-canopy-web"})
    assert r.status_code == 200, r.content
    assert r.json()["sent"] == 1
    [push] = sent
    assert push["user"] == "owner"
    assert push["url"] == AWS_URL, "tapping must open the AWS page with the code filled in"
    # Who, what, where and why, readable from the lock screen.
    assert push["title"] == "hal needs AWS sign-in (labs)"
    assert push["body"].startswith(
        "On jj-mbp: confirm the 5xx alarm cause in /ecs/labs-jj-canopy-web.")
    # A dozing phone must wake for it, and the push service holds it only as
    # long as the device code lives.
    assert push["urgency"] == "high"
    assert push["ttl"] == 600


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_request_that_does_not_say_why_is_refused(owner, runner, sent, reason):
    r = _post(owner, runner, {"url": AWS_URL, "reason": reason})
    assert r.status_code == 422, r.content
    assert "reason" in r.json()["detail"]
    assert sent == []


def test_the_regional_device_page_is_accepted(owner, runner, sent):
    url = "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH"
    assert _post(owner, runner, {"url": url}).status_code == 200


@pytest.mark.parametrize("url", [
    "https://evil.example.com/?user_code=ABCD-EFGH",
    "http://commcare-connect.awsapps.com/start/#/device",       # not https
    "https://commcare-connect.awsapps.com.evil.com/start/",    # suffix trick
    "https://user:pw@commcare-connect.awsapps.com/start/",      # userinfo
    "https://commcare-connect.awsapps.com:8443/start/",         # port
    "https://signin.aws.amazon.com/",                          # AWS, but not a device page
    "javascript:alert(1)",
])
def test_nothing_but_an_aws_device_page_reaches_a_phone(owner, runner, sent, url):
    r = _post(owner, runner, {"url": url})
    assert r.status_code == 422, (url, r.content)
    assert sent == []


def test_an_unknown_provider_is_refused(owner, runner, sent):
    assert _post(owner, runner, {"provider": "gcp", "url": AWS_URL}).status_code == 422
    assert sent == []


def test_someone_else_cannot_put_a_request_on_the_owners_phone(runner, sent):
    """An approved device code grants access to whoever started it, so the
    ability to send one to the owner must already be the owner's."""
    stranger = User.objects.create_user("stranger", "s@dimagi.com", "pw")
    r = _post(stranger, runner, {"url": AWS_URL})
    assert r.status_code == 404
    assert sent == []


def test_no_registered_device_reads_as_zero_so_the_box_falls_back(owner, runner, monkeypatch):
    monkeypatch.setattr(push_services, "send_to_user", lambda user, **kw: 0)
    r = _post(owner, runner, {"url": AWS_URL})
    assert r.status_code == 200
    assert r.json()["sent"] == 0


def _mine(user):
    c = Client()
    c.force_login(user)
    r = c.get("/api/harness/sign-in-requests/mine")
    assert r.status_code == 200, r.content
    return r.json()


def _result(user, runner, request_id, outcome):
    c = Client()
    c.force_login(user)
    return c.post(f"/api/harness/runners/{runner.id}/sign-in-requests/{request_id}/result",
                  data=json.dumps({"outcome": outcome}), content_type="application/json")


def test_an_open_request_waits_on_the_owners_supervisor_feed(owner, runner, sent):
    """The push can be accepted and never shown; the feed is where the owner
    still finds it."""
    rid = _post(owner, runner, {"url": AWS_URL, "label": "labs", "requested_by": "ada",
                                "reason": "move ace-web to its own Redis DB"}).json()["id"]
    [row] = _mine(owner)
    assert row["id"] == rid
    assert row["url"] == AWS_URL
    assert (row["runner_name"], row["label"], row["requested_by"]) == ("jj-mbp", "labs", "ada")
    assert row["reason"] == "move ace-web to its own Redis DB"
    assert row["sent"] == 1


def test_nobody_but_the_owner_sees_the_request(owner, runner, sent):
    """Approving signs the box in as the approver, so the link is the owner's alone."""
    _post(owner, runner, {"url": AWS_URL})
    stranger = User.objects.create_user("stranger", "s@dimagi.com", "pw")
    assert _mine(stranger) == []
    assert _result(stranger, runner, SignInRequest.objects.get().pk, "approved").status_code == 404


@pytest.mark.parametrize("outcome", ["approved", "failed", "expired"])
def test_the_outcome_takes_it_off_the_feed_and_drops_the_url(owner, runner, sent, outcome):
    rid = _post(owner, runner, {"url": AWS_URL}).json()["id"]
    r = _result(owner, runner, rid, outcome)
    assert r.status_code == 200, r.content
    assert r.json() == {"id": rid, "status": outcome}
    assert _mine(owner) == []
    row = SignInRequest.objects.get(pk=rid)
    assert row.url == "", "a spent or dead code is not kept"
    assert row.reason, "who asked and why stays as the audit line"
    # A second report does not rewrite the first.
    assert _result(owner, runner, rid, "failed").json()["status"] == outcome


def test_an_unknown_outcome_is_refused(owner, runner, sent):
    rid = _post(owner, runner, {"url": AWS_URL}).json()["id"]
    assert _result(owner, runner, rid, "maybe").status_code == 422
    assert len(_mine(owner)) == 1


def test_a_request_its_box_never_reported_expires_with_its_code(owner, runner, sent):
    """The box can die mid-wait. The feed must not then offer a dead link."""
    rid = _post(owner, runner, {"url": AWS_URL}).json()["id"]
    SignInRequest.objects.filter(pk=rid).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _mine(owner) == []
    row = SignInRequest.objects.get(pk=rid)
    assert (row.status, row.url) == (SignInRequest.EXPIRED, "")


def test_a_runner_handed_to_someone_else_stops_showing_the_old_owner(owner, runner, sent):
    _post(owner, runner, {"url": AWS_URL})
    runner.owner = User.objects.create_user("next", "n@dimagi.com", "pw")
    runner.save(update_fields=["owner"])
    assert _mine(owner) == []
