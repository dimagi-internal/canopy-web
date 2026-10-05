"""PUT|DELETE /api/agents/{slug}/runner-rules/{source}[?actor=] — one rule at a time,
and every routing write gated on the runners it ADDS (#1143).

The case: ACE's rules route Matt's and Ada's work to the fleet's cloud boxes
(jjackson's, in `dimagi`) and Sarvesh's work to his own laptop (his, in
`connect`). Saving ALL the rules re-checked every runner, so on 2026-10-05 no one
could save ACE's rules at all, not even unchanged: jjackson can't vouch for
Sarvesh's laptop, and Sarvesh can't vouch for the cloud boxes.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet(client):
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com")
    sarvesh = User.objects.create_user(username="st", email="stewari@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj,
                                       parent=dimagi)
    for ws in (dimagi, connect):
        WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=connect, user=sarvesh, role=WorkspaceMembership.EDITOR)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=connect, owner=jj)
    AgentAdmin.objects.create(agent=ace, user=sarvesh, granted_by=jj)
    now = timezone.now()

    def box(name, owner, ws):
        return Runner.objects.create(name=name, kind=Runner.CLOUD, owner=owner, workspace=ws,
                                     status=Runner.ONLINE, last_heartbeat_at=now, capabilities={})

    ec2_1, ec2_2 = box("cloud-ec2-1", jj, dimagi), box("cloud-ec2-2", jj, dimagi)
    st_mbp = box("st-mbp", sarvesh, connect)

    def rule(source, actor, *runners):
        for rank, r in enumerate(runners):
            RunnerAssignment.objects.create(agent=ace, runner=r, rank=rank, source=source,
                                            actor=actor, strict=True)

    rule("ace_web", "mtheis@dimagi.com", ec2_1)
    rule("ace_web", "stewari@dimagi.com", st_mbp)
    rule("api", "", ec2_1)
    return {"client": client, "jj": jj, "sarvesh": sarvesh, "ace": ace,
            "ec2_1": ec2_1, "ec2_2": ec2_2, "st_mbp": st_mbp}


def _rules(ace):
    out: dict = {}
    for a in RunnerAssignment.objects.filter(agent=ace).exclude(source="").order_by("rank"):
        out.setdefault((a.source, a.actor), []).append(a.runner.name)
    return out


def _put(client, source, *runners, actor=None, **body):
    url = f"/api/agents/ace/runner-rules/{source}" + (f"?actor={actor}" if actor is not None else "")
    return client.put(url, data={"runners": [{"runner_id": str(r.id)} for r in runners],
                                 "strict": True, **body}, content_type="application/json")


def _wholesale(client, ace):
    rows: dict = {}
    for a in RunnerAssignment.objects.filter(agent=ace).exclude(source="").order_by("rank"):
        rule = rows.setdefault((a.source, a.actor), {"source": a.source, "actor": a.actor,
                                                    "strict": a.strict, "turn_mode": a.turn_mode,
                                                    "runners": []})
        rule["runners"].append({"runner_id": str(a.runner_id), "enabled": a.enabled})
    return list(rows.values())


# ── the bug itself ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("who", ["jj", "sarvesh"])
def test_anyone_with_the_agent_can_resave_its_rules_unchanged(fleet, who):
    fleet["client"].force_login(fleet[who])
    res = fleet["client"].put("/api/agents/ace/runner-rules",
                              data={"rules": _wholesale(fleet["client"], fleet["ace"])},
                              content_type="application/json")
    assert res.status_code == 200, res.content


def test_one_rule_changes_and_no_other_rule_is_touched(fleet):
    fleet["client"].force_login(fleet["jj"])
    res = _put(fleet["client"], "ace_web", fleet["ec2_2"], fleet["ec2_1"], actor="mtheis@dimagi.com")
    assert res.status_code == 200, res.content
    assert _rules(fleet["ace"]) == {
        ("ace_web", "mtheis@dimagi.com"): ["cloud-ec2-2", "cloud-ec2-1"],
        ("ace_web", "stewari@dimagi.com"): ["st-mbp"],
        ("api", ""): ["cloud-ec2-1"],
    }


# ── the gate still holds on what a write ADDS ───────────────────────────────

def test_adding_a_box_you_do_not_administer_is_refused(fleet):
    fleet["client"].force_login(fleet["jj"])
    res = _put(fleet["client"], "ace_web", fleet["ec2_1"], fleet["st_mbp"], actor="mtheis@dimagi.com")
    assert res.status_code == 403
    assert "st-mbp" in res.json()["detail"]
    assert _rules(fleet["ace"])[("ace_web", "mtheis@dimagi.com")] == ["cloud-ec2-1"]


def test_the_wholesale_save_still_refuses_an_added_box(fleet):
    fleet["client"].force_login(fleet["sarvesh"])
    rules = _wholesale(fleet["client"], fleet["ace"])
    for r in rules:
        if r["actor"] == "stewari@dimagi.com":
            r["runners"].append({"runner_id": str(fleet["ec2_2"].id), "enabled": True})
    res = fleet["client"].put("/api/agents/ace/runner-rules", data={"rules": rules},
                              content_type="application/json")
    # Sarvesh is not in `dimagi`, so to him that box is unknown, not forbidden: a
    # refusal must not confirm that a runner exists.
    assert res.status_code == 422
    assert str(fleet["ec2_2"].id) in res.json()["detail"]
    assert _rules(fleet["ace"])[("ace_web", "stewari@dimagi.com")] == ["st-mbp"]


def test_a_runner_admin_who_does_not_own_the_box_may_add_it(fleet):
    RunnerAdmin.objects.create(runner=fleet["ec2_2"], user=fleet["sarvesh"])
    fleet["client"].force_login(fleet["sarvesh"])
    res = _put(fleet["client"], "ace_web", fleet["st_mbp"], fleet["ec2_2"], actor="stewari@dimagi.com")
    assert res.status_code == 200, res.content


def test_a_box_the_caller_cannot_see_is_unknown_not_forbidden(fleet):
    stranger = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    theirs = Runner.objects.create(name="mal-box", kind=Runner.CLOUD, owner=stranger,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   capabilities={})
    fleet["client"].force_login(fleet["jj"])
    assert _put(fleet["client"], "email", theirs).status_code == 422


# ── rule addressing ─────────────────────────────────────────────────────────

def test_a_rule_with_no_person_is_addressable(fleet):
    fleet["client"].force_login(fleet["jj"])
    assert _put(fleet["client"], "api", fleet["ec2_2"], fleet["ec2_1"]).status_code == 200
    assert _rules(fleet["ace"])[("api", "")] == ["cloud-ec2-2", "cloud-ec2-1"]


def test_the_person_is_normalized(fleet):
    fleet["client"].force_login(fleet["jj"])
    res = _put(fleet["client"], "email", fleet["ec2_1"], actor="Matt%20%3CMTheis@Dimagi.com%3E")
    assert res.status_code == 200, res.content
    assert ("email", "mtheis@dimagi.com") in _rules(fleet["ace"])


@pytest.mark.parametrize("source,actor", [("carrier-pigeon", ""), ("canopy_scheduler", "a@dimagi.com")])
def test_an_impossible_rule_is_refused(fleet, source, actor):
    fleet["client"].force_login(fleet["jj"])
    assert _put(fleet["client"], source, fleet["ec2_1"], actor=actor).status_code == 422


def test_a_rule_needs_a_runner(fleet):
    fleet["client"].force_login(fleet["jj"])
    assert _put(fleet["client"], "api").status_code == 422


def test_delete_removes_exactly_one_rule(fleet):
    fleet["client"].force_login(fleet["jj"])
    res = fleet["client"].delete("/api/agents/ace/runner-rules/ace_web?actor=mtheis@dimagi.com")
    assert res.status_code == 204
    assert _rules(fleet["ace"]) == {("ace_web", "stewari@dimagi.com"): ["st-mbp"],
                                    ("api", ""): ["cloud-ec2-1"]}
    # Idempotent.
    assert fleet["client"].delete(
        "/api/agents/ace/runner-rules/ace_web?actor=mtheis@dimagi.com").status_code == 204


# ── auto: setting it is for admins; keeping it is not ───────────────────────

@pytest.fixture
def editor(fleet):
    ed = get_user_model().objects.create_user(username="ed", email="ed@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ace"].workspace, user=ed,
                                       role=WorkspaceMembership.EDITOR)
    RunnerAssignment.objects.filter(agent=fleet["ace"], source="api").update(turn_mode="auto")
    fleet["client"].force_login(ed)
    return ed


def test_an_editor_may_resave_an_auto_rule_an_admin_set(fleet, editor):
    res = fleet["client"].put("/api/agents/ace/runner-rules",
                              data={"rules": _wholesale(fleet["client"], fleet["ace"])},
                              content_type="application/json")
    assert res.status_code == 200, res.content


def test_an_editor_may_not_turn_a_rule_auto(fleet, editor):
    res = _put(fleet["client"], "ace_web", fleet["ec2_1"], actor="mtheis@dimagi.com",
               turn_mode="auto")
    assert res.status_code == 403
