"""Declaring what a runner's owner vouches for (`zdr`).

canopy cannot check a declaration, so the whole value is in WHO may make one:
a person, from the web app — never the box itself, which authenticates with its
owner's PAT.
"""
from __future__ import annotations

import json

import pytest
from canopy_sdk import contract
from django.contrib.auth.models import User
from django.test import Client

from apps.events.models import Event
from apps.harness.models import Runner, RunnerAdmin, RunnerFlag
from apps.harness.runner_requirements import UNSATISFIABLE
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def admin():
    return User.objects.create_user("adm", "adm@dimagi.com", "pw")


@pytest.fixture()
def stranger():
    return User.objects.create_user("str", "str@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner, admin, stranger):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    for u in (admin, stranger):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return ws


@pytest.fixture()
def runner(owner, admin, workspace):
    r = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=workspace)
    RunnerAdmin.objects.create(runner=r, user=admin, granted_by=owner)
    return r


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def owner_client(owner):
    return _client(owner)


@pytest.fixture()
def admin_client(admin):
    return _client(admin)


@pytest.fixture()
def stranger_client(stranger):
    return _client(stranger)


@pytest.fixture()
def pat_client(owner):
    raw, _ = PersonalToken.create_for_user(user=owner, label="runner")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _put(c, runner, flags):
    return c.put(f"/api/harness/runners/{runner.id}/flags", json.dumps({"flags": flags}),
                 content_type="application/json")


def test_the_runner_owner_declares_zdr(owner_client, runner):
    r = _put(owner_client, runner, ["zdr"])
    assert r.status_code == 200 and r.json()["flags"] == ["zdr"]
    assert RunnerFlag.objects.get(runner=runner).declared_by.email == "jj@dimagi.com"


def test_a_runner_admin_may_declare_it(admin_client, runner):
    assert _put(admin_client, runner, ["zdr"]).status_code == 200


def test_someone_who_cannot_administer_gets_404(stranger_client, runner):
    assert _put(stranger_client, runner, ["zdr"]).status_code == 404
    assert not RunnerFlag.objects.exists()


def test_a_token_can_declare_it(pat_client, runner):
    # A token acts as its user (a runner admin here): MCP can do what the web app can.
    assert _put(pat_client, runner, ["zdr"]).status_code == 200
    assert RunnerFlag.objects.exists()


def test_an_unknown_flag_is_422(owner_client, runner):
    assert _put(owner_client, runner, ["nope"]).status_code == 422
    assert not RunnerFlag.objects.exists()


def test_the_malformed_sentinel_is_not_declarable(owner_client, runner):
    assert _put(owner_client, runner, [UNSATISFIABLE]).status_code == 422
    assert not RunnerFlag.objects.exists()


def test_the_set_is_replaced_and_withdrawal_deletes(owner_client, runner):
    _put(owner_client, runner, ["zdr"])
    assert _put(owner_client, runner, []).json()["flags"] == []
    assert not RunnerFlag.objects.filter(runner=runner).exists()


def test_redeclaring_keeps_the_original_declarer(owner_client, admin_client, runner):
    _put(owner_client, runner, ["zdr"])
    _put(admin_client, runner, ["zdr"])
    assert RunnerFlag.objects.get(runner=runner).declared_by.email == "jj@dimagi.com"


def test_declaring_and_withdrawing_record_events(owner_client, runner):
    _put(owner_client, runner, ["zdr"])
    assert Event.objects.filter(kind="runner.flag_declared").count() == 1
    _put(owner_client, runner, [])
    assert Event.objects.filter(kind="runner.flag_withdrawn").count() == 1


def test_heartbeat_cannot_set_flags(pat_client, runner):
    r = pat_client.post(f"/api/harness/runners/{runner.id}/heartbeat",
                        json.dumps({"flags": ["zdr"]}), content_type="application/json")
    assert r.status_code == 200, r.content
    assert not RunnerFlag.objects.exists()
    assert r.json()["flags"] == []


def test_list_runners_includes_flags(owner_client, runner):
    RunnerFlag.objects.create(runner=runner, flag="zdr")
    rows = owner_client.get("/api/harness/runners/").json()
    row = next(x for x in rows if x["id"] == str(runner.id))
    assert row["flags"] == ["zdr"]
    assert row["known_flags"] == sorted(contract.RUNNER_FLAGS)


def test_the_reply_says_what_the_caller_may_do_with_the_runner(owner_client, admin_client, runner):
    """RunnerOut's defaults are True, so an unstamped reply showed an admin who
    is not the owner the drill/pause controls that then 404."""
    body = _put(admin_client, runner, ["zdr"]).json()
    assert body["can_manage"] is False and body["can_administer"] is True
    body = _put(owner_client, runner, ["zdr"]).json()
    assert body["can_manage"] is True and body["can_administer"] is True
