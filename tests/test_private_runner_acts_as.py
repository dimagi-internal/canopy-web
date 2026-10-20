"""A private runner is a Runner, and its owner can make it act as a lower role (canopy-web#1705).

`Runner.private` takes the box out of the fleet: only what its owner puts on it
(pins, bound sessions) and the local sessions on it. `Runner.acts_as` (`editor` /
`viewer`) lowers the role everything from that box is judged as — its turns, its
credential reads, the local sessions on it — so an agent's owner can test the
non-admin path (license, manual cap, refusal) on their own Mac.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentCredential
from apps.harness import services
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


def _make_private(client, box, acts_as=""):
    return client.put(f"/api/harness/runners/{box.pk}/private",
                      data={"private": True, "acts_as": acts_as}, content_type="application/json")


def _open_local(client, box, session="s1", **extra):
    pr = client.post("/api/private-runners/", data={"name": "jj-mbp", "runner": str(box.pk)},
                     content_type="application/json")
    assert pr.status_code in (200, 201), pr.content
    return client.post("/api/harness/local-turns/", data={
        "agent": "hal", "private_runner": pr.json()["id"], "claude_session_id": session, **extra,
    }, content_type="application/json")


# --- the setting ----------------------------------------------------------------

def test_the_owner_makes_their_runner_private_and_picks_a_role(world):
    res = _make_private(_pat(world["jj"]), world["box"], "editor")
    assert res.status_code == 200, res.content
    assert (res.json()["private"], res.json()["acts_as"]) == (True, "editor")
    world["box"].refresh_from_db()
    assert world["box"].directed_only()  # no fleet work once private


@pytest.mark.parametrize("body,status", [
    ({"private": True, "acts_as": "admin"}, 422),     # not a cap: only lowers
    ({"private": False, "acts_as": "editor"}, 422),   # a cap needs a private runner
])
def test_the_setting_is_validated(world, body, status):
    res = _pat(world["jj"]).put(f"/api/harness/runners/{world['box'].pk}/private", data=body,
                               content_type="application/json")
    assert res.status_code == status


def test_only_the_owner_may_change_it(world):
    from apps.harness.models import RunnerAdmin

    RunnerAdmin.objects.create(runner=world["box"], user=world["other"])
    assert _make_private(_pat(world["other"]), world["box"]).status_code == 404


def test_turning_private_off_clears_the_role(world):
    c = _pat(world["jj"])
    _make_private(c, world["box"], "viewer")
    res = c.put(f"/api/harness/runners/{world['box'].pk}/private", data={"private": False},
                content_type="application/json")
    assert (res.json()["private"], res.json()["acts_as"]) == (False, "")


# --- the cap --------------------------------------------------------------------

def test_a_cap_only_lowers(world):
    jj = world["jj"]
    assert world["hal"].is_admin(jj)
    role_cap.apply(jj, "editor")
    assert not world["hal"].is_admin(jj)
    role_cap.apply(jj, "editor")
    role_cap.apply(jj, "viewer")
    role_cap.apply(jj, "editor")  # a weaker cap never loosens a stronger one
    assert role_cap.cap_of(jj) == "viewer"
    assert role_cap.capped_role("viewer", "editor") == "viewer"


def test_the_owner_naming_their_capped_runner_is_judged_as_an_editor(world):
    _make_private(_pat(world["jj"]), world["box"], "editor")
    named = _pat(world["jj"], HTTP_X_CANOPY_RUNNER=str(world["box"].pk))
    body = named.get("/api/agents/hal/credentials/access").json()
    # No admin read: an editor reads only through a license, during their own turn.
    assert (body["may_resolve"], body["via"]) == (False, None)
    plain = _pat(world["jj"]).get("/api/agents/hal/credentials/access").json()
    assert (plain["may_resolve"], plain["via"]) == (True, "admin")


def test_someone_elses_runner_named_in_the_header_caps_nothing(world):
    world["box"].private, world["box"].acts_as = True, "viewer"
    world["box"].save()
    hers = Runner.objects.create(name="aath-mbp", kind=Runner.EMDASH, owner=world["other"],
                                 workspace=world["ws"], private=True, acts_as="viewer")
    named = _pat(world["jj"], HTTP_X_CANOPY_RUNNER=str(hers.pk))
    assert named.get("/api/agents/hal/credentials/access").json()["via"] == "admin"


# --- local sessions on the capped runner ----------------------------------------

def test_a_local_turn_on_a_capped_runner_is_an_editors(world):
    c = _pat(world["jj"])
    _make_private(c, world["box"], "editor")
    res = _open_local(c, world["box"], turn_mode="auto")
    assert res.status_code == 201, res.content
    turn = Turn.objects.get(pk=res.json()["turn_id"])
    assert turn.origin_ref["acts_as"] == "editor"
    assert turn.turn_mode == "manual"  # auto is an agent admin's, and a capped owner is not one
    assert "acting as editor" in turn.turn_mode_basis
    lic = world["hal"].licenses.get(user=world["jj"])
    assert lic.status == "active" and lic.basis == "editor"
    assert PrivateRunner.objects.get(pk=turn.private_runner_id).runner_id == world["box"].pk


def test_its_reads_go_out_under_the_license_and_keep_the_cap(world):
    c = _pat(world["jj"])
    _make_private(c, world["box"], "editor")
    turn_id = _open_local(c, world["box"]).json()["turn_id"]
    # No X-Canopy-Runner on the read: the turn's own cap still holds.
    read = _pat(world["jj"], HTTP_X_CANOPY_TURN_ID=turn_id).get("/api/agents/hal/credentials/resolve")
    assert read.status_code == 200, read.content
    assert read.json()["values"]["hal-key"] == SECRET
    from apps.agents.models import AgentCredentialRead
    assert set(AgentCredentialRead.objects.values_list("via", flat=True)) == {"license"}


def test_a_viewer_cap_is_refused_a_local_turn(world):
    c = _pat(world["jj"])
    _make_private(c, world["box"], "viewer")
    res = _open_local(c, world["box"])
    assert res.status_code == 403
    assert "may not use hal locally" in res.content.decode()


def test_a_revoked_license_refuses_the_capped_owner_too(world):
    from apps.agents import licenses

    c = _pat(world["jj"])
    _make_private(c, world["box"], "editor")
    licenses.revoke(world["hal"], world["jj"], by=world["jj"])
    assert _open_local(c, world["box"]).status_code == 403


# --- remote turns the private runner claims --------------------------------------

def test_a_capped_private_runner_claims_its_owners_pinned_turn_as_an_editor(world):
    jj, box = world["jj"], world["box"]
    _make_private(_pat(jj), box, "editor")
    res = _pat(jj).post("/api/harness/turns/", {
        "agent_slug": "hal", "origin": "api", "idempotency_key": "k1", "prompt": "ping",
        "runner_id": str(box.pk), "turn_mode": "auto",
    }, content_type="application/json")
    assert res.status_code == 201, res.content
    box.refresh_from_db()
    claimed = services.claim_next_turn(box)
    assert claimed is not None
    assert claimed.origin_ref["acts_as"] == "editor"
    assert claimed.turn_mode == "manual"
    from apps.harness import caller_context
    assert caller_context.relationship(claimed, world["hal"]) == "member"


def test_a_private_runner_takes_no_routed_fleet_work(world):
    from apps.harness.models import RunnerAssignment

    jj, box = world["jj"], world["box"]
    RunnerAssignment.objects.create(agent=world["hal"], runner=box, rank=0)
    _make_private(_pat(jj), box)
    _pat(world["other"]).post("/api/harness/turns/", {
        "agent_slug": "hal", "origin": "api", "idempotency_key": "k2", "prompt": "ping",
    }, content_type="application/json")
    box.refresh_from_db()
    assert services.claim_next_turn(box) is None


# --- no CLI on the box ------------------------------------------------------------

def test_making_it_private_links_a_registration_the_runner_reads(world):
    res = _make_private(_pat(world["jj"]), world["box"], "editor")
    pr_id = res.json()["private_runner_id"]
    assert pr_id
    pr = PrivateRunner.objects.get(pk=pr_id)
    assert (pr.owner, pr.runner_id, pr.name) == (world["jj"], world["box"].pk, "jj-mbp-cdp")
    # Idempotent: switching the role keeps the same registration.
    again = _make_private(_pat(world["jj"]), world["box"], "viewer")
    assert again.json()["private_runner_id"] == pr_id
    assert PrivateRunner.objects.filter(owner=world["jj"]).count() == 1
