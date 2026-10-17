"""Workspace health + analytics: one answer for "set up right", one fact log for usage.

GET /api/workspaces/{slug}/health and /analytics read the tree the topology views
read, and compose the answers that already exist — the setup checklist, the vault
verdict, the analytics fact log — rather than keeping second opinions.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent, AgentCredential
from apps.agents.setup import setup_steps
from apps.agents.vault_health import vault_health
from apps.events import activity, usage
from apps.events.models import Activity, TurnUsage
from apps.harness import initiator as _initiator
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _get(user, path):
    c = Client()
    c.force_login(user)
    return c.get(path)


@pytest.fixture
def tree():
    ceo, staff, builder = _user("ceo@dimagi.com"), _user("staff@dimagi.com"), _user("b@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=ceo,
                                      shared_op_vault="Fleet")
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=ceo, parent=dimagi)
    WorkspaceMembership.objects.create(workspace=dimagi, user=ceo, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=dimagi, user=staff, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=connect, user=builder, role=WorkspaceMembership.EDITOR)
    hal = Agent.objects.create(
        slug="hal", name="Hal", workspace=connect, owner=builder,
        credential_source=Agent.CANOPY_WEB, runtime_secrets=["gh", "base-url", "old"],
        runtime_sources={"base-url": {"value": "https://x"}, "old": {"op": "op://V/old/credential"}},
    )
    AgentCredential.objects.create(agent=hal, name="gh")
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=ceo)
    return {"ceo": ceo, "staff": staff, "builder": builder, "hal": hal, "eva": eva}


def _turn(agent, key, user=None):
    who = _initiator.for_user(user, via="test", assurance="verified") if user else _initiator.system(via="test")
    turn, _ = services.enqueue_turn(initiator=who, agent=agent, origin=Turn.ORIGIN_API,
                                    idempotency_key=key, prompt="go", enqueued_by=user)
    return turn


# ---- the vault verdict and the checklist read the same thing --------------------


def test_vault_health_classifies_each_declared_secret(tree):
    vh = vault_health(tree["hal"])
    assert {r["name"]: r["status"] for r in vh["refs"]} == {
        "gh": "stored", "base-url": "literal", "old": "1password"}
    assert vh["state"] == "moving"
    assert vh["in_1password"] == ["old"]


def test_a_canopy_web_agent_is_not_asked_for_a_1password_vault(tree):
    step = next(s for s in setup_steps(tree["hal"]) if s["key"] == "vault")
    assert step["status"] == "todo"
    assert "still only in 1Password: old" in step["detail"]
    assert step["where"] == "web" and step["href"].endswith("#credentials")

    AgentCredential.objects.create(agent=tree["hal"], name="old")
    step = next(s for s in setup_steps(tree["hal"]) if s["key"] == "vault")
    assert step["status"] == "done"
    assert vault_health(tree["hal"])["state"] == "canopy-web"


# ---- health -------------------------------------------------------------------


def test_owner_sees_every_agent_in_the_tree_with_its_checklist(tree):
    r = _get(tree["ceo"], "/api/workspaces/dimagi/health")
    assert r.status_code == 200, r.content
    body = r.json()
    assert [(w["slug"], w["depth"]) for w in body["workspaces"]] == [("dimagi", 0), ("connect", 1)]
    assert body["workspaces"][1]["shared_vault"] == {"vault": "Fleet", "key_set": False,
                                                     "inherited_from": "dimagi"}
    rows = {a["slug"]: a for a in body["agents"]}
    assert set(rows) == {"eva", "hal"}
    # The rows ARE the checklist: same keys, same order, same statuses.
    assert [s["key"] for s in rows["hal"]["steps"]] == [s["key"] for s in setup_steps(tree["hal"])]
    assert rows["hal"]["vault"]["state"] == "moving"
    assert rows["hal"]["owner_email"] == "b@dimagi.com"
    # The CEO is a workspace owner, so an admin of every agent below.
    assert rows["hal"]["can_fix"] is True
    assert body["totals"]["agents"] == 2 and body["totals"]["vault_moving"] == 1


def test_health_is_the_admin_tier(tree):
    assert _get(tree["staff"], "/api/workspaces/dimagi/health").status_code in (403, 404)


# ---- usage from the transcript ------------------------------------------------


def _result(cost, inp, out, model="claude-opus-5-5"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "total_cost_usd": cost,
                       "usage": {"input_tokens": inp, "output_tokens": out,
                                 "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1},
                       "modelUsage": {model: {"costUSD": cost}}})


def test_usage_sums_every_run_of_a_turn():
    raw = "\n".join([json.dumps({"type": "assistant", "message": {}}), _result(0.5, 10, 20),
                     "not json", _result(0.25, 1, 2)])
    u = usage.parse(raw)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.runs) == (11, 22, 14, 2)
    assert u.cost_usd == Decimal("0.75") and u.model == "claude-opus-5-5"
    assert usage.parse('{"type": "assistant"}') is None


def test_a_transcript_batch_with_a_result_records_the_turns_usage(tree, django_capture_on_commit_callbacks):
    turn = _turn(tree["hal"], "u1", tree["builder"])
    with django_capture_on_commit_callbacks(execute=True):
        services.append_transcript(turn, ['{"type": "system", "subtype": "init", "model": "m"}'])
    assert not TurnUsage.objects.exists()
    with django_capture_on_commit_callbacks(execute=True):
        services.append_transcript(turn, [_result(1.5, 100, 50)])
    row = TurnUsage.objects.get()
    assert (row.workspace_id, row.agent_slug, row.user, row.input_tokens) == ("connect", "hal", tree["builder"], 100)
    assert row.cost_usd == Decimal("1.5")


# ---- the fact log -------------------------------------------------------------


def test_turns_emit_enqueued_and_finished_facts(tree):
    turn = _turn(tree["hal"], "f1", tree["builder"])
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    services.finish_turn(turn, status=Turn.DONE)
    facts = list(Activity.objects.filter(ref=str(turn.pk)).order_by("at").values_list(
        "kind", "workspace_id", "agent_slug", "actor__email", "actor_kind"))
    assert facts == [
        (activity.TURN_ENQUEUED, "connect", "hal", "b@dimagi.com", "user"),
        (activity.TURN_FINISHED, "connect", "hal", "b@dimagi.com", "user"),
    ]


def test_an_unknown_kind_is_refused_and_a_tenantless_fact_dropped(tree):
    with pytest.raises(ValueError):
        activity.emit("made.up", workspace="dimagi")
    assert activity.emit(activity.MCP_CALL, workspace="") is None


def test_activity_has_one_writer():
    """Every fact goes through `activity.emit`, so every producer stamps the same
    who/where/what/when."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "apps"
    offenders = [str(p) for p in root.rglob("*.py")
                 if "tests" not in p.parts and "migrations" not in p.parts
                 and p.name != "activity.py"
                 and ("Activity.objects.create" in p.read_text() or "Activity.objects.bulk_create" in p.read_text())]
    assert offenders == []


# ---- analytics ----------------------------------------------------------------


def test_analytics_counts_turns_people_and_tokens_across_the_tree(tree, django_capture_on_commit_callbacks):
    t1 = _turn(tree["hal"], "a1", tree["builder"])
    _turn(tree["eva"], "a2", tree["ceo"])
    _turn(tree["eva"], "a3")  # canopy itself: a turn, not a person
    with django_capture_on_commit_callbacks(execute=True):
        services.append_transcript(t1, [_result(2.0, 300, 40)])

    body = _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=7").json()
    assert body["totals"]["turns"] == 3
    assert body["totals"]["active_users"] == 2
    assert body["totals"]["input_tokens"] == 300 and body["totals"]["cost_usd"] == 2.0
    assert body["totals"]["turns_with_usage"] == 1
    assert len(body["series"]) == 7 and body["series"][-1]["turns"] == 3
    users = {u["email"]: u for u in body["users"]}
    assert users["b@dimagi.com"]["turns"] == 1 and users["b@dimagi.com"]["input_tokens"] == 300
    assert users["b@dimagi.com"]["top_agents"] == ["hal"]
    agents = {a["agent"]: a for a in body["agents"]}
    assert agents["eva"]["turns"] == 2 and agents["hal"]["cost_usd"] == 2.0

    only_root = _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=7&tree=false").json()
    assert only_root["totals"]["turns"] == 2


def test_backfill_seeds_facts_once(tree):
    from django.core.management import call_command

    turn = _turn(tree["hal"], "b1", tree["builder"])
    Activity.objects.all().delete()
    call_command("backfill_activity", "--no-usage")
    call_command("backfill_activity", "--no-usage")
    assert Activity.objects.filter(kind=activity.TURN_ENQUEUED, ref=str(turn.pk)).count() == 1
