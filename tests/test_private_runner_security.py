"""A private runner speaks for its owner, so only its owner may extend that trust.

A private runner's token signs in AS its owner: it opens their local turns and reads
secrets under their license (canopy-web#1705). So who may put a key on such a box,
which registration the box is, and what a SHARED box's token can do to its owner's
local sessions are all decisions that must stay with the owner.
"""
from __future__ import annotations

import importlib

import pytest
from canopy_sdk.keys import header_jwk
from cryptography.hazmat.primitives.asymmetric import ec
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentCredential, AgentVaultWrap
from apps.api import route_gates
from apps.events.models import SettingChange
from apps.harness import runner_auth
from apps.harness.models import PrivateRunner, Runner, RunnerAdmin, RunnerDeviceKey, Turn
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    admin = User.objects.create_user("aath", "aath@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=admin, role=WorkspaceMembership.EDITOR)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj)
    box = Runner.objects.create(name="jj-mbp-cdp", kind=Runner.EMDASH, owner=jj, workspace=ws,
                                host="jj-mbp.local", status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now(),
                                capabilities={"agents": ["hal"], "sessions": True})
    RunnerAdmin.objects.create(runner=box, user=admin)
    return {"ws": ws, "hal": hal, "jj": jj, "admin": admin, "box": box}


def _pat(user) -> Client:
    raw, _tok = PersonalToken.create_for_user(user=user, label="cli")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _browser(user) -> Client:
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


def _set_private(user, box, private=True):
    return _pat(user).put(f"/api/harness/runners/{box.pk}/private",
                          data={"private": private}, content_type="application/json")


def _register_key(user, box):
    jwk = header_jwk(ec.generate_private_key(ec.SECP256R1()))
    return _pat(user).post("/api/runner-device-keys/", data={
        "runner": str(box.pk), "public_jwk": jwk, "kind": "secure-enclave", "label": "k"},
        content_type="application/json")


def _box_token(box):
    from apps.harness.models import RunnerAccessToken

    key = RunnerDeviceKey.objects.create(runner=box, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=f"jkt-{box.pk}", status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


# --- device keys on a private runner: the owner's alone ----------------------------

def test_a_runner_admin_registers_a_key_on_a_shared_box_but_not_a_private_one(world):
    assert _register_key(world["admin"], world["box"]).status_code == 201
    _set_private(world["jj"], world["box"])
    res = _register_key(world["admin"], world["box"])
    assert res.status_code == 403 and "private runner" in res.json()["detail"]
    assert _register_key(world["jj"], world["box"]).status_code == 201


def test_a_key_an_admin_left_pending_is_not_theirs_to_approve_once_private(world):
    key_id = _register_key(world["admin"], world["box"]).json()["id"]
    _set_private(world["jj"], world["box"])
    assert _browser(world["admin"]).post(f"/api/runner-device-keys/{key_id}/approve").status_code == 403
    assert RunnerDeviceKey.objects.get(pk=key_id).status == RunnerDeviceKey.PENDING
    assert _browser(world["jj"]).post(f"/api/runner-device-keys/{key_id}/approve").status_code == 200


def test_an_admin_still_sees_a_private_runners_keys_but_cannot_manage_them(world):
    _register_key(world["jj"], world["box"])
    _set_private(world["jj"], world["box"])
    rows = _pat(world["admin"]).get(f"/api/runner-device-keys/?runner={world['box'].pk}").json()
    assert len(rows) == 1 and rows[0]["can_manage"] is False
    mine = _pat(world["jj"]).get(f"/api/runner-device-keys/?runner={world['box'].pk}").json()
    assert mine[0]["can_manage"] is True


def test_only_the_owner_is_asked_to_approve_a_private_runners_key(world):
    from apps.harness import device_key_notify

    assert {u.pk for u in device_key_notify.approvers(world["box"])} == {world["jj"].pk, world["admin"].pk}
    _set_private(world["jj"], world["box"])
    world["box"].refresh_from_db()
    assert [u.pk for u in device_key_notify.approvers(world["box"])] == [world["jj"].pk]


# --- a runner token never touches /api/private-runners --------------------------------

def test_a_private_runners_token_cannot_register_or_list_private_runners(world):
    _set_private(world["jj"], world["box"])
    world["box"].refresh_from_db()
    as_box = _box_token(world["box"])
    assert as_box.get("/api/private-runners/").status_code == 403
    assert as_box.post("/api/private-runners/", data={"name": "other", "runner": str(world["box"].pk)},
                       content_type="application/json").status_code == 403
    assert not runner_auth.reaches("POST", "/api/private-runners", world["box"].pk, private=True)
    assert runner_auth.reaches("POST", "/api/harness/local-turns", world["box"].pk, private=True)


# --- a shared box's token never reports on its owner's local turns -------------------

def test_a_shared_runners_token_cannot_finish_its_owners_local_turn(world):
    jj, box = world["jj"], world["box"]
    pr = PrivateRunner.objects.create(owner=jj, name="jj-mbp", runner=box)
    opened = _pat(jj).post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": str(pr.pk), "claude_session_id": "s1"},
        content_type="application/json")
    assert opened.status_code == 201, opened.content
    tid = opened.json()["turn_id"]
    as_box = _box_token(box)  # box.private is False: a fleet box
    assert as_box.post(f"/api/harness/turns/{tid}/transcript", data={"lines": ["{}"], "batch_id": "b"},
                       content_type="application/json").status_code == 404
    assert as_box.post(f"/api/harness/turns/{tid}/finish", data={"status": "done"},
                       content_type="application/json").status_code == 404
    assert Turn.objects.get(pk=tid).status == Turn.RUNNING
    # The person's own token still does.
    assert _pat(jj).post(f"/api/harness/turns/{tid}/finish", data={"status": "done"},
                         content_type="application/json").status_code == 200


def test_a_private_runners_token_reports_only_on_turns_opened_on_it(world):
    jj, box = world["jj"], world["box"]
    _set_private(jj, box)
    elsewhere = PrivateRunner.objects.create(owner=jj, name="jj-other-laptop")
    tid = _pat(jj).post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": str(elsewhere.pk), "claude_session_id": "s2"},
        content_type="application/json").json()["turn_id"]
    box.refresh_from_db()
    as_box = _box_token(box)
    assert as_box.post(f"/api/harness/turns/{tid}/finish", data={"status": "done"},
                       content_type="application/json").status_code == 404
    assert as_box.post(f"/api/harness/local-turns/{tid}/renew").status_code == 404


# --- PUT /private: audited both ways, and shared again means unlinked --------------

def test_making_it_shared_again_is_audited_and_unlinks_the_registration(world):
    jj, box = world["jj"], world["box"]
    pr_id = _set_private(jj, box).json()["private_runner_id"]
    res = _set_private(jj, box, private=False)
    assert res.status_code == 200 and res.json()["private"] is False
    assert PrivateRunner.objects.get(pk=pr_id).runner_id is None
    changes = [c for row in SettingChange.objects.filter(operation="set_runner_private")
               for c in row.changes]
    assert {"scope": "runner-private:jj-mbp-cdp", "before": {"private": True},
            "after": {"private": False}} in changes
    assert {"scope": "runner-private:jj-mbp-cdp", "before": {"private": False},
            "after": {"private": True}} in changes


def test_making_it_private_again_relinks_its_own_registration(world):
    jj, box = world["jj"], world["box"]
    pr_id = _set_private(jj, box).json()["private_runner_id"]
    _set_private(jj, box, private=False)
    # Unlinked, it is now just a registration of that name from this host: it is adopted.
    assert _set_private(jj, box).json()["private_runner_id"] == pr_id


# --- making it private never adopts another machine's registration ------------------

def test_making_it_private_does_not_adopt_a_namesake_from_another_host(world):
    jj, box = world["jj"], world["box"]
    cli = PrivateRunner.objects.create(owner=jj, name="jj-mbp-cdp", host="some-other-laptop")
    pr_id = _set_private(jj, box).json()["private_runner_id"]
    assert str(cli.pk) != pr_id
    cli.refresh_from_db()
    assert cli.runner_id is None
    mine = PrivateRunner.objects.get(pk=pr_id)
    assert (mine.name, mine.runner_id) == ("jj-mbp-cdp-2", box.pk)


def test_making_it_private_does_not_steal_a_namesake_linked_to_another_runner(world):
    jj, box = world["jj"], world["box"]
    other = Runner.objects.create(name="jj-other", kind=Runner.EMDASH, owner=jj, workspace=world["ws"],
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    theirs = PrivateRunner.objects.create(owner=jj, name="jj-mbp-cdp", host="jj-mbp.local", runner=other)
    pr_id = _set_private(jj, box).json()["private_runner_id"]
    theirs.refresh_from_db()
    assert theirs.runner_id == other.pk and str(theirs.pk) != pr_id


def test_it_adopts_an_unlinked_registration_of_its_name_from_its_own_host(world):
    jj, box = world["jj"], world["box"]
    cli = PrivateRunner.objects.create(owner=jj, name="jj-mbp-cdp", host="jj-mbp.local")
    assert _set_private(jj, box).json()["private_runner_id"] == str(cli.pk)


# --- retiring a registration hands its runner back to the fleet ---------------------

def test_retiring_a_linked_registration_makes_its_runner_shared(world):
    jj, box = world["jj"], world["box"]
    pr_id = _set_private(jj, box).json()["private_runner_id"]
    res = _pat(jj).post(f"/api/private-runners/{pr_id}/retire")
    assert res.status_code == 200, res.content
    box.refresh_from_db()
    pr = PrivateRunner.objects.get(pk=pr_id)
    assert box.private is False and pr.runner_id is None and pr.retired_at is not None
    row = SettingChange.objects.get(operation="retire_private_runner")
    assert {"scope": "runner-private:jj-mbp-cdp", "before": {"private": True},
            "after": {"private": False}} in row.changes


# --- 1Password agents 0059 made zero-knowledge ----------------------------------------

def test_migration_turns_zk_off_only_for_1password_agents_holding_nothing(world):
    ws, jj = world["ws"], world["jj"]
    Agent.objects.create(slug="bare", workspace=ws, owner=jj, zk_enabled=True, zk_kv=0,
                                credential_source=Agent.ONEPASSWORD)
    holding = Agent.objects.create(slug="holding", workspace=ws, owner=jj, zk_enabled=True, zk_kv=0,
                                   credential_source=Agent.ONEPASSWORD)
    AgentCredential.objects.create(agent=holding, name="x", value_enc="")
    wrapped = Agent.objects.create(slug="wrapped", workspace=ws, owner=jj, zk_enabled=True, zk_kv=0,
                                   credential_source=Agent.ONEPASSWORD)
    AgentVaultWrap.objects.create(agent=wrapped, kv=1, kind=AgentVaultWrap.RECOVERY, wrap={})
    Agent.objects.create(slug="keyed", workspace=ws, owner=jj, zk_enabled=True, zk_kv=2,
                                 credential_source=Agent.ONEPASSWORD)
    Agent.objects.create(slug="vault", workspace=ws, owner=jj, zk_enabled=True, zk_kv=0,
                                 credential_source=Agent.CANOPY_WEB)
    mig = importlib.import_module("apps.agents.migrations.0069_no_zk_for_1password_agents")
    mig.forwards(django_apps, None)
    flags = dict(Agent.objects.filter(slug__in=["bare", "holding", "wrapped", "keyed", "vault"])
                 .values_list("slug", "zk_enabled"))
    assert flags == {"bare": False, "holding": True, "wrapped": True, "keyed": True, "vault": True}


def test_migration_moves_agents_with_no_1password_vault_onto_canopy_vault(world):
    ws, jj = world["ws"], world["jj"]
    Agent.objects.create(slug="novault", workspace=ws, owner=jj, credential_source=Agent.ONEPASSWORD)
    Agent.objects.create(slug="legacy", workspace=ws, owner=jj, credential_source=Agent.ONEPASSWORD,
                         op_vault="Agent-Legacy")
    held = Agent.objects.create(slug="held", workspace=ws, owner=jj, credential_source=Agent.ONEPASSWORD)
    AgentCredential.objects.create(agent=held, name="x", value_enc="")
    mig = importlib.import_module("apps.agents.migrations.0070_vaultless_agents_on_canopy_vault")
    mig.forwards(django_apps, None)
    rows = {a.slug: (a.credential_source, a.zk_enabled, a.zk_kv)
            for a in Agent.objects.filter(slug__in=["novault", "legacy", "held"])}
    assert rows["novault"] == (Agent.CANOPY_WEB, True, 0)
    assert rows["legacy"][0] == Agent.ONEPASSWORD and rows["held"][0] == Agent.ONEPASSWORD


def test_a_1password_agent_resolves_without_a_device_proof_even_if_zk_is_on(world):
    hal = world["hal"]
    Agent.objects.filter(pk=hal.pk).update(zk_enabled=True, zk_kv=0, credential_source=Agent.ONEPASSWORD)
    res = _pat(world["jj"]).get("/api/agents/hal/credentials/resolve")
    assert res.status_code == 200, res.content
    # A canopy-web-vault agent still demands one.
    Agent.objects.filter(pk=hal.pk).update(credential_source=Agent.CANOPY_WEB)
    res = _pat(world["jj"]).get("/api/agents/hal/credentials/resolve")
    assert res.status_code == 401 and res.json()["error"] == "device_proof_required"


# --- the route manifest names the license principal ----------------------------------

def test_resolve_declares_the_license_principal():
    assert "license" in route_gates.VOCABULARY
    from apps.agents.api_credentials import resolve_agent_credentials

    assert "license" in route_gates.gate_of(resolve_agent_credentials).labels
