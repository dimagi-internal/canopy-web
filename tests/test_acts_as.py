"""Running as a lower role, and the private runner (canopy-web#1705).

`acts_as` (`editor` / `viewer`) is a property of ONE turn — or of one client's
requests (`X-Canopy-Acts-As`), which is how a runner puts itself in a lower mode —
never a setting left on a machine. Everything about the turn is judged at that role:
whether it may start, its mode, its envelope, its credential reads. So anyone, an
agent included, can run the non-admin path (license, manual cap, refusal) for real.
`Runner.private` takes a box out of the fleet: only what its owner puts on it and
the local sessions on it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentCredential
from apps.harness import claim
from apps.harness.models import PrivateRunner, Runner, Turn
from apps.tokens.models import PersonalToken
from apps.workspaces import role_cap
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()
SECRET = "hal-value-not-a-real-token"


@pytest.fixture
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    other = User.objects.create_user("aath", "aath@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=other, role=WorkspaceMembership.EDITOR)
    # No published interface: an editor gets the whole agent (manual), a viewer nothing.
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, turn_mode=Agent.AUTO,
                               runtime_secrets=["hal-key"], credential_source=Agent.CANOPY_WEB)
    AgentCredential.objects.create(agent=hal, name="hal-key", value_enc="")
    from apps.agents import services as agent_services
    agent_services.set_agent_credentials(hal, {"hal-key": SECRET}, user=jj)
    box = Runner.objects.create(name="jj-mbp-cdp", kind=Runner.EMDASH, owner=jj, workspace=ws,
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                capabilities={"agents": ["hal"], "sessions": True})
    return {"ws": ws, "hal": hal, "jj": jj, "other": other, "box": box}


def _pat(user, **headers) -> Client:
    raw, _tok = PersonalToken.create_for_user(user=user, label="cli")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}", **headers)


def _make_private(client, box):
    return client.put(f"/api/harness/runners/{box.pk}/private",
                      data={"private": True}, content_type="application/json")


def _dispatch(client, box=None, key="k1", **extra):
    return client.post("/api/harness/turns/", {
        "agent_slug": "hal", "origin": "api", "idempotency_key": key, "prompt": "ping",
        **({"runner_id": str(box.pk)} if box is not None else {}), **extra,
    }, content_type="application/json")


def _open_local(client, box, session="s1", **extra):
    pr = client.post("/api/private-runners/", data={"name": "jj-mbp", "runner": str(box.pk)},
                     content_type="application/json")
    assert pr.status_code in (200, 201), pr.content
    return client.post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": pr.json()["id"], "claude_session_id": session, **extra,
    }, content_type="application/json")


# --- the private setting ----------------------------------------------------------

def test_the_owner_makes_their_runner_private_and_nothing_else(world):
    res = _make_private(_pat(world["jj"]), world["box"])
    assert res.status_code == 200, res.content
    assert res.json()["private"] is True and "acts_as" not in res.json()
    world["box"].refresh_from_db()
    assert world["box"].directed_only()  # no fleet work once private


def test_only_the_owner_may_change_it(world):
    from apps.harness.models import RunnerAdmin

    RunnerAdmin.objects.create(runner=world["box"], user=world["other"])
    assert _make_private(_pat(world["other"]), world["box"]).status_code == 404


def test_making_it_private_links_a_registration_the_runner_reads(world):
    res = _make_private(_pat(world["jj"]), world["box"])
    pr_id = res.json()["private_runner_id"]
    pr = PrivateRunner.objects.get(pk=pr_id)
    assert (pr.owner, pr.runner_id, pr.name) == (world["jj"], world["box"].pk, "jj-mbp-cdp")
    assert _make_private(_pat(world["jj"]), world["box"]).json()["private_runner_id"] == pr_id
    assert PrivateRunner.objects.filter(owner=world["jj"]).count() == 1


def test_a_private_runner_takes_no_routed_fleet_work(world):
    from apps.harness.models import RunnerAssignment

    jj, box = world["jj"], world["box"]
    RunnerAssignment.objects.create(agent=world["hal"], runner=box, rank=0)
    _make_private(_pat(jj), box)
    _dispatch(_pat(world["other"]), key="k2")
    box.refresh_from_db()
    assert claim.claim_next_turn(box) is None


# --- the cap --------------------------------------------------------------------

def test_a_cap_only_lowers(world):
    jj = world["jj"]
    assert world["hal"].is_admin(jj)
    role_cap.apply(jj, "editor")
    assert not world["hal"].is_admin(jj)
    role_cap.apply(jj, "viewer")
    role_cap.apply(jj, "editor")  # a weaker cap never loosens a stronger one
    assert role_cap.cap_of(jj) == "viewer"
    assert role_cap.capped_role("viewer", "editor") == "viewer"
    assert role_cap.stronger("editor", "") == "editor" and role_cap.stronger("editor", "viewer") == "viewer"


# --- a turn started as a lower role -----------------------------------------------

def test_a_turn_started_as_an_editor_runs_as_one(world):
    jj, box = world["jj"], world["box"]
    res = _dispatch(_pat(jj), box, acts_as="editor")
    assert res.status_code == 201, res.content
    claimed = claim.claim_next_turn(box)
    assert claimed.origin_ref["acts_as"] == "editor"
    assert claimed.turn_mode == "manual"  # auto is an agent admin's
    from apps.harness import caller_context
    assert caller_context.relationship(claimed, world["hal"]) == "member"
    # The cap is the turn's: the same person's next turn acts as themselves.
    Turn.objects.filter(pk=claimed.pk).update(status=Turn.DONE)
    _dispatch(_pat(jj), box, key="k-plain")
    plain = claim.claim_next_turn(box)
    assert "acts_as" not in plain.origin_ref


def test_a_capped_turn_may_not_ask_for_auto(world):
    res = _dispatch(_pat(world["jj"]), world["box"], acts_as="editor", turn_mode="auto")
    assert res.status_code == 403


def test_a_turn_started_as_a_viewer_is_refused_like_a_viewers(world):
    res = _dispatch(_pat(world["jj"]), world["box"], acts_as="viewer")
    assert res.status_code in (403, 404)
    assert not Turn.objects.exists()


def test_a_client_cannot_assert_a_cap_through_origin_ref(world):
    # Harmless (a cap only lowers) but the server stamps it, so the record is true.
    res = _dispatch(_pat(world["jj"]), world["box"], origin_ref={"acts_as": "viewer"})
    assert res.status_code == 201
    assert "acts_as" not in Turn.objects.get().origin_ref


def test_its_reads_on_the_owners_private_runner_go_out_under_the_license(world):
    jj, box = world["jj"], world["box"]
    _make_private(_pat(jj), box)
    _dispatch(_pat(jj), box, acts_as="editor")
    box.refresh_from_db()
    turn = claim.claim_next_turn(box)
    as_box = Client(HTTP_AUTHORIZATION=f"Bearer {_box_token(box)}")
    access = as_box.get("/api/agents/hal/credentials/access", HTTP_X_CANOPY_TURN_ID=str(turn.pk)).json()
    assert access["via"] != "admin"


# --- a client in a lower mode (X-Canopy-Acts-As) -----------------------------------

def test_a_request_in_a_lower_mode_is_judged_at_it(world):
    capped = _pat(world["jj"], HTTP_X_CANOPY_ACTS_AS="editor")
    body = capped.get("/api/agents/hal/credentials/access").json()
    assert (body["may_resolve"], body["via"]) == (False, None)
    plain = _pat(world["jj"]).get("/api/agents/hal/credentials/access").json()
    assert (plain["may_resolve"], plain["via"]) == (True, "admin")
    assert _pat(world["jj"], HTTP_X_CANOPY_ACTS_AS="admin").get(
        "/api/agents/hal/credentials/access").json()["via"] == "admin"  # not a cap: ignored


def test_a_runner_claiming_in_a_lower_mode_runs_its_owners_turns_in_it(world):
    jj, box = world["jj"], world["box"]
    _dispatch(_pat(jj), box)
    turn = claim.claim_next_turn(box, acts_as="editor")
    assert turn.origin_ref["acts_as"] == "editor" and turn.turn_mode == "manual"
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    # Never loosens a cap the turn was started with.
    _dispatch(_pat(jj), box, key="k-capped", acts_as="editor")
    t2 = claim.claim_next_turn(box, acts_as="editor")
    assert t2.origin_ref["acts_as"] == "editor"


def test_a_runners_mode_never_reaches_someone_elses_turn(world):
    from apps.harness.models import RunnerAdmin

    box = world["box"]
    RunnerAdmin.objects.create(runner=box, user=world["other"])
    res = _dispatch(_pat(world["other"]), box, key="k-other")
    assert res.status_code == 201, res.content
    turn = claim.claim_next_turn(box, acts_as="viewer")
    assert "acts_as" not in turn.origin_ref


def test_the_rest_claim_reads_the_header(world):
    jj, box = world["jj"], world["box"]
    _dispatch(_pat(jj), box)
    as_box = Client(HTTP_AUTHORIZATION=f"Bearer {_box_token(box)}", HTTP_X_CANOPY_ACTS_AS="editor")
    res = as_box.post(f"/api/harness/runners/{box.pk}/claim")
    assert res.status_code == 200, res.content
    assert Turn.objects.get().origin_ref["acts_as"] == "editor"


# --- local sessions opened as a lower role -----------------------------------------

def test_a_local_turn_opened_as_an_editor_is_an_editors(world):
    c = _pat(world["jj"])
    res = _open_local(c, world["box"], turn_mode="auto", acts_as="editor")
    assert res.status_code == 201, res.content
    turn = Turn.objects.get(pk=res.json()["turn_id"])
    assert turn.origin_ref["acts_as"] == "editor"
    assert turn.turn_mode == "manual"
    assert "acting as editor" in turn.turn_mode_basis
    lic = world["hal"].licenses.get(user=world["jj"])
    assert lic.status == "active" and lic.basis == "editor"


def test_its_reads_go_out_under_the_license_and_keep_the_cap(world):
    c = _pat(world["jj"])
    turn_id = _open_local(c, world["box"], acts_as="editor").json()["turn_id"]
    read = _pat(world["jj"], HTTP_X_CANOPY_TURN_ID=turn_id).get("/api/agents/hal/credentials/resolve")
    assert read.status_code == 200, read.content
    assert read.json()["values"]["hal-key"] == SECRET
    from apps.agents.models import AgentCredentialRead
    assert set(AgentCredentialRead.objects.values_list("via", flat=True)) == {"license"}


def test_a_local_turn_opened_as_a_viewer_is_refused(world):
    res = _open_local(_pat(world["jj"]), world["box"], acts_as="viewer")
    assert res.status_code == 403
    assert "may not use hal locally" in res.content.decode()


def test_a_revoked_license_refuses_the_capped_owner_too(world):
    from apps.agents.credentials import licenses

    c = _pat(world["jj"])
    licenses.revoke(world["hal"], world["jj"], by=world["jj"])
    assert _open_local(c, world["box"], acts_as="editor").status_code == 403


# --- the box's own key signs its owner's local sessions in: no PAT on a private runner ---

def _box_token(box):
    from apps.harness.models import RunnerAccessToken, RunnerDeviceKey

    key = RunnerDeviceKey.objects.create(runner=box, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=f"jkt-{box.pk}", status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    return raw


def test_a_private_runners_key_opens_and_closes_its_owners_local_turn(world):
    jj, box = world["jj"], world["box"]
    pr_id = _make_private(_pat(jj), box).json()["private_runner_id"]
    as_box = Client(HTTP_AUTHORIZATION=f"Bearer {_box_token(box)}")
    res = as_box.post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": pr_id, "claude_session_id": "s-key", "acts_as": "editor"},
        content_type="application/json")
    assert res.status_code == 201, res.content
    turn = Turn.objects.get(pk=res.json()["turn_id"])
    assert turn.initiator_user == jj and turn.initiator_assurance == "private-runner-key"
    assert turn.origin_ref["acts_as"] == "editor" and turn.turn_mode == "manual"
    read = as_box.get("/api/agents/hal/credentials/resolve", HTTP_X_CANOPY_TURN_ID=str(turn.pk))
    assert read.status_code == 200, read.content
    assert read.json()["values"]["hal-key"] == SECRET
    from apps.agents.models import AgentCredentialRead
    assert set(AgentCredentialRead.objects.values_list("via", flat=True)) == {"license"}
    renew = as_box.post(f"/api/harness/local-turns/{turn.pk}/renew")
    assert renew.status_code == 200, renew.content
    done = as_box.post(f"/api/harness/turns/{turn.pk}/finish", data={"status": "done"},
                       content_type="application/json")
    assert done.status_code == 200, done.content
    turn.refresh_from_db()
    assert turn.status == Turn.DONE


def test_a_shared_runners_key_opens_no_local_session(world):
    jj, box = world["jj"], world["box"]
    pr = PrivateRunner.objects.create(owner=jj, name="jj-mbp", runner=box)
    as_box = Client(HTTP_AUTHORIZATION=f"Bearer {_box_token(box)}")
    res = as_box.post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": str(pr.pk), "claude_session_id": "s-shared"},
        content_type="application/json")
    assert res.status_code == 403


def test_a_private_runners_key_opens_only_its_own_registration(world):
    jj, box = world["jj"], world["box"]
    _make_private(_pat(jj), box)
    elsewhere = PrivateRunner.objects.create(owner=jj, name="jj-other-laptop")
    as_box = Client(HTTP_AUTHORIZATION=f"Bearer {_box_token(box)}")
    res = as_box.post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": str(elsewhere.pk), "claude_session_id": "s-other"},
        content_type="application/json")
    assert res.status_code == 403
