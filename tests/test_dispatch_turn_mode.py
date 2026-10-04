"""A dispatch may name the MODE its turn runs in (`TurnIn.turn_mode`).

Owner request, 2026-10-04: an agent's admins — human or agent — choose, per
dispatch, whether the turn runs manual or auto (and, with `runner_id`, on which
box), instead of standing per-person routing rules. The request is the TOP rung
of `apps/harness/turn_mode.py`, above every rule and the agent's own switch.

The security-relevant cases: `auto` only from the agent's owner/admin (workspace
owners included) on a verified credential; never on email (a runner posts it on
a stranger's behalf); re-checked at claim, so a revoked admin's queued auto runs
manual. `manual` is open to anyone who may enqueue.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.harness import caller_context, services
from apps.harness import turn_mode as modes
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.tokens.models import AppCredential, DelegatedToken, PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()
ADA = "ada@dimagi-ai.com"


@pytest.fixture
def fleet():
    jj = User.objects.create_user("jj", "jj@dimagi.com")          # workspace owner
    owner = User.objects.create_user("own", "own@dimagi.com")     # hal's owner
    ada = User.objects.create_user("ada", ADA)                     # explicit admin
    member = User.objects.create_user("mem", "mem@dimagi.com")    # plain editor
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    for u, role in ((jj, WorkspaceMembership.OWNER), (owner, WorkspaceMembership.EDITOR),
                    (ada, WorkspaceMembership.EDITOR), (member, WorkspaceMembership.EDITOR)):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=role)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner,
                               turn_mode=Agent.MANUAL)
    AgentAdmin.objects.create(agent=hal, user=ada, granted_by=owner)
    now = timezone.now()
    box = Runner.objects.create(name="haldimagi-mbp-cdp", kind=Runner.EMDASH, owner=jj, workspace=ws,
                                status=Runner.ONLINE, last_heartbeat_at=now,
                                capabilities={"agents": ["hal"]})
    other = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=jj, workspace=ws,
                                  status=Runner.ONLINE, last_heartbeat_at=now,
                                  capabilities={"agents": ["hal"]})
    RunnerAssignment.objects.create(agent=hal, runner=other, rank=0)
    return {"ws": ws, "hal": hal, "jj": jj, "owner": owner, "ada": ada, "member": member,
            "box": box, "other": other}


def _session(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _pat(user) -> Client:
    raw, _tok = PersonalToken.create_for_user(user=user, label="cli")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _post(client, key="k1", **extra):
    body = {"agent_slug": "hal", "origin": "api", "idempotency_key": key, "prompt": "ping"}
    body.update(extra)
    return client.post("/api/harness/turns/", body, content_type="application/json")


# --- who may ask --------------------------------------------------------------

def test_an_admin_on_a_pat_may_request_auto(fleet):
    r = _post(_pat(fleet["ada"]), turn_mode="auto")
    assert r.status_code == 201, r.content
    body = r.json()
    assert (body["requested_turn_mode"], body["requested_turn_mode_by_email"]) == ("auto", ADA)
    t = Turn.objects.get()
    assert (t.requested_turn_mode, t.requested_turn_mode_by) == ("auto", fleet["ada"])


def test_the_agents_owner_may_request_auto(fleet):
    assert _post(_session(fleet["owner"]), turn_mode="auto").status_code == 201


def test_a_workspace_owner_may_request_auto(fleet):
    assert _post(_session(fleet["jj"]), turn_mode="auto").status_code == 201


def test_a_plain_member_gets_403_for_auto_and_nothing_is_queued(fleet):
    r = _post(_session(fleet["member"]), turn_mode="auto")
    assert r.status_code == 403
    assert "owner or admins" in r.json()["detail"]
    assert not Turn.objects.exists()


def test_a_plain_member_may_request_manual(fleet):
    r = _post(_session(fleet["member"]), turn_mode="manual")
    assert r.status_code == 201, r.content
    assert r.json()["requested_turn_mode"] == "manual"


def test_a_widget_delegated_token_cannot_unlock_auto_even_for_an_admin(fleet):
    app = AppCredential.objects.create(name="labs", workspace=fleet["ws"])
    raw, _ = DelegatedToken.issue(app=app, user=fleet["ada"], ttl_seconds=600)
    r = _post(Client(HTTP_AUTHORIZATION=f"Bearer {raw}"), turn_mode="auto")
    assert r.status_code in (403, 404), r.content
    assert not Turn.objects.filter(requested_turn_mode="auto").exists()


def test_a_project_turn_rejects_turn_mode(fleet):
    r = _session(fleet["jj"]).post(
        "/api/w/connect/harness/turns/",
        {"project": "canopy-web", "origin": "api", "idempotency_key": "p1",
         "turn_mode": "manual"},
        content_type="application/json",
    )
    assert r.status_code == 422
    assert "agent turn" in r.json()["detail"]


def test_an_email_turn_rejects_turn_mode(fleet):
    r = _post(_session(fleet["jj"]), origin="email", turn_mode="auto",
              origin_ref={"from": "x@y.org", "thread_id": "t1", "subject": "s"})
    assert r.status_code == 422
    assert "email" in r.json()["detail"]


def test_an_unknown_mode_is_a_422(fleet):
    assert _post(_session(fleet["jj"]), turn_mode="yolo").status_code == 422


def test_omitting_turn_mode_changes_nothing(fleet):
    r = _post(_session(fleet["member"]))
    assert r.status_code == 201
    assert r.json()["requested_turn_mode"] == ""
    assert Turn.objects.get().requested_turn_mode_by is None


# --- the request wins, and keeps winning --------------------------------------

def test_requested_auto_wins_over_the_agents_manual_switch(fleet):
    _post(_pat(fleet["ada"]), turn_mode="auto")
    claimed = services.claim_next_turn(fleet["other"])
    assert claimed.turn_mode == "auto"
    assert claimed.turn_mode_basis == f"dispatch by {ADA} (admin)"


def test_requested_manual_wins_over_an_actor_auto_rule(fleet):
    hal = fleet["hal"]
    RunnerAssignment.objects.create(agent=hal, runner=fleet["other"], rank=0,
                                    source=Turn.ORIGIN_API, actor=ADA, turn_mode="auto")
    _post(_pat(fleet["ada"]), turn_mode="manual")
    claimed = services.claim_next_turn(fleet["other"])
    assert (claimed.turn_mode, claimed.turn_mode_basis) == (
        "manual", f"dispatch by {ADA} (admin)")


def test_requested_auto_wins_over_an_actor_manual_rule(fleet):
    hal = fleet["hal"]
    hal.turn_mode = Agent.AUTO
    hal.save(update_fields=["turn_mode"])
    RunnerAssignment.objects.create(agent=hal, runner=fleet["other"], rank=0,
                                    source=Turn.ORIGIN_API, actor=ADA, turn_mode="manual")
    _post(_pat(fleet["ada"]), turn_mode="auto")
    assert services.claim_next_turn(fleet["other"]).turn_mode == "auto"


def test_the_request_survives_a_lost_lease_and_reclaim(fleet):
    _post(_pat(fleet["ada"]), turn_mode="auto")
    first = services.claim_next_turn(fleet["other"])
    # Lease lost: back to the queue with the old stamp; the claim decides afresh.
    Turn.objects.filter(pk=first.pk).update(status=Turn.QUEUED, claimed_by=None,
                                            turn_mode="", turn_mode_basis="")
    again = services.claim_next_turn(fleet["other"])
    assert again.pk == first.pk
    assert (again.turn_mode, again.turn_mode_basis) == ("auto", f"dispatch by {ADA} (admin)")
    assert modes.for_turn(again, fresh=True).mode == "auto"


def test_a_revoked_admin_s_queued_auto_runs_manual_and_says_why(fleet):
    _post(_pat(fleet["ada"]), turn_mode="auto")
    AgentAdmin.objects.filter(user=fleet["ada"]).delete()
    claimed = services.claim_next_turn(fleet["other"])
    assert claimed.turn_mode == "manual"
    assert "auto withheld" in claimed.turn_mode_basis


def test_the_envelope_carries_the_dispatch_basis(fleet):
    _post(_pat(fleet["ada"]), turn_mode="auto")
    turn = Turn.objects.get()
    # Before a claim the envelope resolves live; after, it reads the stamp.
    assert caller_context.build(turn)["turn_mode"] == {
        "mode": "auto", "basis": f"dispatch by {ADA} (admin)"}
    claimed = services.claim_next_turn(fleet["other"])
    assert caller_context.build(claimed)["turn_mode"] == {
        "mode": "auto", "basis": f"dispatch by {ADA} (admin)"}


# --- pin + mode together ------------------------------------------------------

def test_a_pin_and_a_mode_together(fleet):
    box = fleet["box"]
    r = _post(_pat(fleet["ada"]), turn_mode="manual", runner_id=str(box.id))
    assert r.status_code == 201, r.content
    body = r.json()
    assert (body["pinned_runner_id"], body["pinned_runner_name"]) == (str(box.id), box.name)
    assert body["requested_turn_mode"] == "manual"
    # Only the pinned box may claim it, and it carries the requested mode.
    assert services.claim_next_turn(fleet["other"]) is None
    claimed = services.claim_next_turn(box)
    assert (claimed.turn_mode, claimed.turn_mode_basis) == (
        "manual", f"dispatch by {ADA} (admin)")
    listed = _session(fleet["jj"]).get("/api/harness/turns/?agent=hal").json()
    assert listed[0]["pinned_runner_name"] == box.name
    assert listed[0]["requested_turn_mode_by_email"] == ADA
