"""A runner's full Claude logins are a LIST: add one, remove one, reorder them.

Each login lives on the box (its refresh token rotates, so it can never travel);
canopy-web holds which ones the box should keep and the order it falls through
them on a usage cap. The box reports which are signed in, and as whom.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.harness.models import Runner, RunnerMint

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def runner(owner):
    return Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner)


def _url(runner, tail=""):
    return f"/api/harness/runners/{runner.id}/logins{tail}"


def _keys(resp):
    return [login["key"] for login in resp.json()["logins"]]


def _add(client, runner):
    resp = client.post(_url(runner))
    assert resp.status_code == 200, resp.content
    return resp


def test_adding_a_login_appends_it_and_starts_its_sign_in(client, runner):
    first = _keys(_add(client, runner))
    keys = _keys(_add(client, runner))
    assert len(keys) == 2 and keys[0] == first[0]
    mint = RunnerMint.objects.filter(runner=runner).order_by("-created_at").first()
    assert (mint.slot, mint.login_key) == ("box", keys[1])
    assert client.get(f"/api/harness/runners/{runner.id}/mint").json()["login_key"] == keys[1]


def test_a_new_login_reads_not_signed_in_until_the_box_says_so(client, runner):
    login = _add(client, runner).json()["logins"][0]
    assert login["signed_in"] is False and login["account"] == ""


def test_finishing_the_sign_in_records_the_account_the_box_read_back(client, runner):
    key = _keys(_add(client, runner))[0]
    mint = RunnerMint.objects.get(runner=runner, login_key=key)
    mint.status = RunnerMint.COMPLETING
    mint.save()
    resp = client.post(f"/api/harness/runners/{runner.id}/mint/result",
                       data={"logged_in": True, "account": "ace@dimagi-ai.com"},
                       content_type="application/json")
    assert resp.status_code == 200, resp.content
    login = client.get(f"/api/harness/runners/{runner.id}/credential/status").json()["logins"][0]
    assert login["signed_in"] is True and login["account"] == "ace@dimagi-ai.com"


def test_a_sign_in_with_no_login_signs_in_the_default_login(client, runner):
    """A mint from before there was a list signed in the CLI's own directory."""
    client.post(f"/api/harness/runners/{runner.id}/mint", data={"slot": "box"},
                content_type="application/json")
    RunnerMint.objects.filter(runner=runner).update(status=RunnerMint.COMPLETING)
    client.post(f"/api/harness/runners/{runner.id}/mint/result",
                data={"logged_in": True, "account": "a@x.com"}, content_type="application/json")
    logins = client.get(f"/api/harness/runners/{runner.id}/credential/status").json()["logins"]
    assert [(lg["key"], lg["signed_in"]) for lg in logins] == [("default", True)]


def test_removing_a_login_drops_it_from_what_the_box_reads(client, runner):
    a, b = _keys(_add(client, runner)), None
    b = _keys(_add(client, runner))[1]
    resp = client.delete(_url(runner, f"/{a[0]}"))
    assert resp.status_code == 200 and _keys(resp) == [b]
    bundle = client.get(f"/api/harness/runners/{runner.id}/credential").json()
    assert [lg["key"] for lg in bundle["logins"]] == [b]


def test_removing_an_unknown_login_is_a_404(client, runner):
    assert client.delete(_url(runner, "/nope")).status_code == 404


def test_reordering_sets_the_cascade_order(client, runner):
    for _ in range(3):
        _add(client, runner)
    keys = _keys(client.get(f"/api/harness/runners/{runner.id}/credential/status"))
    want = [keys[2], keys[0], keys[1]]
    resp = client.put(_url(runner, "/order"), data={"keys": want}, content_type="application/json")
    assert resp.status_code == 200 and _keys(resp) == want
    bundle = client.get(f"/api/harness/runners/{runner.id}/credential").json()
    assert [lg["key"] for lg in bundle["logins"]] == want


def test_an_order_that_leaves_a_login_out_is_refused(client, runner):
    _add(client, runner)
    keys = _keys(_add(client, runner))
    resp = client.put(_url(runner, "/order"), data={"keys": keys[:1]},
                      content_type="application/json")
    assert resp.status_code == 422


def test_the_box_adopts_a_login_it_already_had_at_the_front(client, runner):
    later = _keys(_add(client, runner))[0]
    resp = client.post(_url(runner, "/report"), data={
        "adopt_default": True,
        "logins": [{"key": "default", "signed_in": True, "account": "a@x.com"},
                   {"key": later, "signed_in": False}],
    }, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert _keys(resp) == ["default", later]
    assert resp.json()["logins"][0]["account"] == "a@x.com"


def test_only_the_runners_admins_touch_its_logins(runner):
    stranger = User.objects.create_user("x", "x@dimagi.com", "pw")
    c = Client()
    c.force_login(stranger)
    assert c.post(_url(runner)).status_code == 404
    assert c.post(_url(runner, "/report"), data={"logins": []},
                  content_type="application/json").status_code == 404
