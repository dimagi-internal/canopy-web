"""A computer used in plain Claude Code (no runner app) gets its own key holder.

Registering a computer for local use (`POST /api/private-runners/`) gives it a
`computer` runner (`Runner.COMPUTER`, `local.ensure_computer_runner`) so it can enrol
a device key, be wrapped an agent's vault key and read a zero-knowledge agent's
secrets under its owner's license. It is no fleet runner: it claims nothing, its
token reaches only its owner's local turns and license reads, and no fleet list,
picker or report shows it. docs/architecture/access.md → "Local use of an agent".
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

from apps.agents.credentials import licenses, zk
from apps.agents.models import Agent
from apps.harness import device_keys, runner_auth
from apps.harness import claim as harness_claim, runners
from apps.harness.models import (
    PrivateRunner,
    Runner,
    RunnerAccessToken,
    RunnerDeviceKey,
    RunnerEnrollment,
    Turn,
)
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
    aath = User.objects.create_user("aath", "aath@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=aath, role=WorkspaceMembership.EDITOR)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, zk_enabled=True)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj, zk_enabled=True)
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                capabilities={"agents": ["hal", "eva"], "sessions": True})
    return {"ws": ws, "jj": jj, "aath": aath, "hal": hal, "eva": eva, "box": box}


def _pat(user) -> Client:
    raw, _tok = PersonalToken.create_for_user(user=user, label="cli")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _browser(user) -> Client:
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


def _register(user, name="aath-mbp"):
    return _pat(user).post("/api/private-runners/", data={"name": name, "host": "aath-mbp.local"},
                           content_type="application/json")


def _enrol(user, runner_id):
    jwk = header_jwk(ec.generate_private_key(ec.SECP256R1()))
    ecdh = header_jwk(ec.generate_private_key(ec.SECP256R1()))
    return _pat(user).post("/api/runner-device-keys/", data={
        "runner": str(runner_id), "public_jwk": jwk, "ecdh_public_jwk": ecdh,
        "kind": "secure-enclave", "label": "aath-mbp"}, content_type="application/json")


def _computer(world, user=None):
    """aath's computer, registered, with an APPROVED key; returns (pr_id, runner, key)."""
    user = user or world["aath"]
    body = _register(user).json()
    key_id = _enrol(user, body["runner_id"]).json()["id"]
    assert _browser(user).post(f"/api/runner-device-keys/{key_id}/approve").status_code == 200
    return body["id"], Runner.objects.get(pk=body["runner_id"]), RunnerDeviceKey.objects.get(pk=key_id)


def _token(key) -> Client:
    raw, _ = RunnerAccessToken.mint(key)
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


# --- registering gives the computer its own runner, once --------------------------

def test_registering_a_computer_returns_a_computer_runner_idempotently(world):
    first = _register(world["aath"])
    assert first.status_code == 201, first.content
    body = first.json()
    box = Runner.objects.get(pk=body["runner_id"])
    assert (box.kind, box.private, box.owner_id, box.workspace_id) == (
        Runner.COMPUTER, True, world["aath"].pk, None)
    assert not RunnerEnrollment.objects.filter(runner=box).exists()
    again = _register(world["aath"])
    assert again.status_code == 200 and again.json()["runner_id"] == body["runner_id"]
    assert Runner.objects.filter(kind=Runner.COMPUTER, owner=world["aath"]).count() == 1
    assert set(body) >= {"id", "name", "host", "runner_id", "created_at", "last_seen_at", "retired_at"}


def test_a_computer_that_runs_the_runner_app_keeps_that_runner(world):
    jj = world["jj"]
    app = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=jj, workspace=world["ws"],
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    res = _pat(jj).post("/api/private-runners/", data={"name": "jj-mbp", "runner": str(app.pk)},
                        content_type="application/json")
    assert res.json()["runner_id"] == str(app.pk)
    assert not Runner.objects.filter(kind=Runner.COMPUTER, owner=jj).exists()


def test_linking_the_runner_app_later_retires_the_computer_runner(world):
    aath = world["aath"]
    body = _register(aath).json()
    app = Runner.objects.create(name="aath-app", kind=Runner.EMDASH, owner=aath, workspace=world["ws"],
                                capabilities={})
    res = _pat(aath).post("/api/private-runners/", data={"name": "aath-mbp", "runner": str(app.pk)},
                          content_type="application/json")
    assert res.json()["runner_id"] == str(app.pk)
    assert Runner.objects.get(pk=body["runner_id"]).status == Runner.RETIRED


def test_retiring_the_computer_retires_its_runner_and_reregistering_makes_a_fresh_one(world):
    pr_id, box, key = _computer(world)
    as_box = _token(key)
    assert _pat(world["aath"]).post(f"/api/private-runners/{pr_id}/retire").status_code == 200
    box.refresh_from_db()
    assert box.status == Runner.RETIRED
    assert not device_keys.runner_serves_agent(box, world["hal"])
    assert as_box.post("/api/harness/local-turns/", data={}, content_type="application/json").status_code == 401
    fresh = _register(world["aath"]).json()
    assert fresh["id"] == pr_id and fresh["runner_id"] not in (None, str(box.pk))


def test_migration_backfills_a_computer_runner_for_unlinked_registrations(world):
    live = PrivateRunner.objects.create(owner=world["aath"], name="old-laptop")
    gone = PrivateRunner.objects.create(owner=world["aath"], name="gone", retired_at=timezone.now())
    mig = importlib.import_module("apps.harness.migrations.0094_runner_kind_computer")
    mig.give_computers_a_runner(django_apps, None)
    live.refresh_from_db()
    gone.refresh_from_db()
    assert live.runner.kind == Runner.COMPUTER and live.runner.private and live.runner.workspace_id is None
    assert gone.runner_id is None


# --- device keys: the owner's alone, eligible exactly under the license -------------

def test_only_the_owner_registers_or_approves_a_key_on_their_computer(world):
    body = _register(world["aath"]).json()
    assert _enrol(world["jj"], body["runner_id"]).status_code == 404  # not jj's box
    key_id = _enrol(world["aath"], body["runner_id"]).json()["id"]
    assert _browser(world["jj"]).post(f"/api/runner-device-keys/{key_id}/approve").status_code == 404
    # The owner, from a browser behind a passkey — not with a token.
    assert _pat(world["aath"]).post(f"/api/runner-device-keys/{key_id}/approve").status_code == 403
    assert _browser(world["aath"]).post(f"/api/runner-device-keys/{key_id}/approve").status_code == 200


def test_a_computer_key_is_vault_eligible_only_for_an_agent_its_owner_is_licensed_for(world):
    _pr, box, key = _computer(world)
    hal, eva = world["hal"], world["eva"]
    assert key not in zk.eligible_device_keys(hal)
    licenses.request(hal, world["aath"])  # an editor gets the whole agent: issued at once
    assert device_keys.runner_serves_agent(box, hal)
    assert key in zk.eligible_device_keys(hal)
    assert not device_keys.runner_serves_agent(box, eva)
    assert key not in zk.eligible_device_keys(eva)
    # Revoked, it is no longer eligible.
    licenses.revoke(hal, world["aath"], by=world["jj"])
    assert key not in zk.eligible_device_keys(hal)


def test_an_admins_computer_key_is_eligible_for_the_agents_they_administer(world):
    _pr, box, key = _computer(world, user=world["jj"])
    assert key in zk.eligible_device_keys(world["hal"]) and key in zk.eligible_device_keys(world["eva"])


# --- its token: local turns and license reads, never the runner protocol ----------

def test_a_computer_token_reaches_local_turns_and_license_reads_only(world):
    pr_id, box, key = _computer(world)
    licenses.request(world["hal"], world["aath"])
    as_box = _token(key)
    opened = as_box.post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": pr_id, "claude_session_id": "s1"},
        content_type="application/json")
    assert opened.status_code == 201, opened.content
    tid = opened.json()["turn_id"]
    assert as_box.post(f"/api/harness/local-turns/{tid}/renew").status_code == 200
    # Reads its agent's secrets under the license, during its own turn — never as a runner.
    access = as_box.get("/api/agents/hal/credentials/access", HTTP_X_CANOPY_TURN_ID=tid).json()
    assert (access["may_resolve"], access["via"]) == (True, "license")
    assert as_box.get("/api/agents/hal/credentials/access").json()["may_resolve"] is False
    # The runner protocol is closed to it.
    for method, path in (("post", f"/api/harness/runners/{box.pk}/claim"),
                         ("post", f"/api/harness/runners/{box.pk}/heartbeat"),
                         ("get", "/api/harness/runners/"),
                         ("get", "/api/agents/"),
                         ("get", "/api/harness/schedules/"),
                         ("post", "/api/runner-device-keys/")):
        assert getattr(as_box, method)(path, content_type="application/json").status_code == 403, path
    assert not runner_auth.reaches_ws(f"/ws/runner/{box.pk}/", box.pk, computer=True)
    assert as_box.post(f"/api/harness/turns/{tid}/finish", data={"status": "done"},
                       content_type="application/json").status_code == 200


def test_a_computer_token_cannot_report_on_a_turn_its_owners_fleet_box_holds(world):
    _pr_id, _box, key = _computer(world, user=world["jj"])
    fleet_turn = Turn.objects.create(agent=world["hal"], status=Turn.RUNNING, claimed_by=world["box"],
                                     idempotency_key="fleet-1", enqueued_by=world["jj"])
    assert _token(key).post(f"/api/harness/turns/{fleet_turn.pk}/finish", data={"status": "done"},
                            content_type="application/json").status_code == 404
    assert Turn.objects.get(pk=fleet_turn.pk).status == Turn.RUNNING


def test_a_computer_never_claims(world):
    _pr, box, _key = _computer(world, user=world["jj"])
    Runner.objects.filter(pk=box.pk).update(status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    box.refresh_from_db()
    Turn.objects.create(agent=world["hal"], status=Turn.QUEUED, idempotency_key="q-1",
                        enqueued_by=world["jj"], pinned_runner=box)
    assert harness_claim.claim_next_turn(box) is None
    # Nor is it reachable as a runner, even by its owner's own token.
    assert _pat(world["jj"]).post(f"/api/harness/runners/{box.pk}/claim").status_code == 404
    assert _pat(world["jj"]).post(f"/api/harness/runners/{box.pk}/workspaces",
                                  data={"workspace": "dimagi"},
                                  content_type="application/json").status_code == 404
    with pytest.raises(runners.EnrollmentRefused):
        runners.enroll_runner(box, "dimagi", by=world["jj"])


# --- no fleet list shows it ---------------------------------------------------------

def test_a_computer_is_in_no_fleet_list_picker_or_report(world):
    from apps.harness import claim, topology
    from apps.realtime.snapshot import supervisor_snapshot

    _pr, box, _key = _computer(world, user=world["jj"])
    jj = _pat(world["jj"])
    ids = {r["id"] for r in jj.get("/api/harness/runners/").json()}
    assert str(world["box"].pk) in ids and str(box.pk) not in ids
    assert str(box.pk) not in {r["id"] for r in jj.get("/api/w/dimagi/harness/runners/").json()}
    assert str(box.pk) not in {r["id"] for r in supervisor_snapshot(world["jj"])["runners"]}
    # Routing pickers refuse it: an agent's runner list and the workspace order.
    res = jj.put("/api/agents/hal/runners", data={"runner_ids": [str(box.pk)]},
                 content_type="application/json")
    assert res.status_code == 422, res.content
    res = jj.put("/api/workspaces/dimagi/runner-order",
                 data={"runners": [{"runner_id": str(box.pk)}]}, content_type="application/json")
    assert res.status_code == 422, res.content
    # Topology / Health.
    built = topology.build(world["ws"], viewer=world["jj"])
    assert str(box.pk) not in {str(r["id"]) for r in built["runners"]}
    # Unclaimable-turn analysis: a turn pinned to it is still diagnosed, never "served" by it.
    Turn.objects.create(agent=world["hal"], status=Turn.QUEUED, idempotency_key="q-2",
                        enqueued_by=world["jj"])
    for row in claim.unclaimable_queued_turns(world["jj"], ws_slugs={"dimagi"}):
        assert box.name not in str(row)
