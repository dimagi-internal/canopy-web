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


def test_health_and_analytics_count_an_agents_turns_the_same_way(tree):
    """One window, one source: Health's "Turns, 30 days" equals Analytics' per-agent
    Turns for 30 days (the first-use review found them disagreeing)."""
    import datetime as dt

    from django.utils import timezone

    _turn(tree["hal"], "w1", tree["builder"])
    old = _turn(tree["hal"], "w2", tree["builder"])
    # 30 days and a few hours ago: outside the 30-UTC-day window either way.
    Activity.objects.filter(ref=str(old.pk)).update(at=timezone.now() - dt.timedelta(days=30, hours=1))
    health = {a["slug"]: a for a in _get(tree["ceo"], "/api/workspaces/dimagi/health").json()["agents"]}
    analytics = {a["agent"]: a for a in _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=30").json()["agents"]}
    assert health["hal"]["turns_30d"] == analytics["hal"]["turns"] == 1
    assert health["hal"]["last_turn_at"] is not None


# ---- usage from interactive transcripts and chat rows -------------------------


def _assistant(msg_id, inp, out, model="claude-opus-5-5"):
    return json.dumps({"type": "assistant", "message": {
        "id": msg_id, "model": model, "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 100}}})


def test_an_interactive_transcript_counts_each_model_message_once():
    # Claude Code writes one record per content block, each repeating its usage.
    raw = "\n".join([_assistant("m1", 5, 50), _assistant("m1", 5, 50), _assistant("m2", 2, 20),
                     json.dumps({"type": "assistant", "message": {"model": "<synthetic>", "usage": {"output_tokens": 9}}})])
    u = usage.parse(raw)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.runs) == (7, 70, 200, 0)
    assert u.cost_usd == 0  # tokens only: an interactive session reports no cost


def test_finishing_an_interactive_turn_records_its_tokens(tree, django_capture_on_commit_callbacks):
    turn = _turn(tree["hal"], "i1", tree["builder"])
    services.append_transcript(turn, [_assistant("m1", 3, 30)])
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        services.finish_turn(turn, status=Turn.DONE)
    row = TurnUsage.objects.get(turn_ref=str(turn.pk))
    assert (row.output_tokens, row.runs) == (30, 0)


def test_a_reported_cost_is_never_replaced_by_a_per_message_estimate(tree, django_capture_on_commit_callbacks):
    turn = _turn(tree["hal"], "i2", tree["builder"])
    with django_capture_on_commit_callbacks(execute=True):
        services.append_transcript(turn, [_assistant("m1", 3, 30), _result(0.4, 3, 30)])
    usage._store(str(turn.pk), turn=turn, workspace="connect", agent=tree["hal"], user=None,
                 at=turn.created_at, usage=usage.from_messages([{"message_id": "x", "output_tokens": 1}]))
    assert TurnUsage.objects.get(turn_ref=str(turn.pk)).cost_usd == Decimal("0.4")


def test_chat_rows_credit_their_tokens_to_the_message_they_answer(tree, django_capture_on_commit_callbacks):
    from apps.canopy_sessions import services as chat
    from apps.canopy_sessions.models import Message, Session

    s = Session.objects.create(workspace_id="connect", agent=tree["hal"], created_by=tree["builder"],
                               origin=Session.ORIGIN_RUNNER, title="t")
    turn = _turn(tree["hal"], "c1", tree["builder"])
    # The person's message arrives through the same path, attributed to its turn.
    chat.persist_transcript_rows(s, [{"index": 10, "role": "user", "text": "q"}], attribute=False)
    Message.objects.filter(session=s, turn_index=10).update(source_turn_id=turn.pk)
    u1 = {"message_id": "m1", "model": "claude-opus-5-5", "input_tokens": 4, "output_tokens": 40,
          "cache_read_tokens": 0, "cache_creation_tokens": 0}
    rows = [{"index": 20, "role": "assistant", "text": "a", "content": {"usage": u1}},
            {"index": 21, "role": "tool_use", "text": "", "content": {"id": "t", "name": "Bash", "input": {}}}]
    with django_capture_on_commit_callbacks(execute=True):
        chat.persist_transcript_rows(s, rows, attribute=False)
    # A later batch of the same answer is summed with it, not instead of it.
    u2 = {**u1, "message_id": "m2", "output_tokens": 5}
    with django_capture_on_commit_callbacks(execute=True):
        chat.persist_transcript_rows(s, [{"index": 30, "role": "assistant", "text": "b", "content": {"usage": u2}}],
                                     attribute=False)
    row = TurnUsage.objects.get(turn_ref=str(turn.pk))
    assert (row.output_tokens, row.user, row.agent_slug) == (45, tree["builder"], "hal")
    # Someone typing straight into the session (no turn) is still counted.
    chat.persist_transcript_rows(s, [{"index": 40, "role": "user", "text": "typed"}], attribute=False)
    Message.objects.filter(session=s, turn_index=40).update(author={"user_id": tree["ceo"].pk})
    with django_capture_on_commit_callbacks(execute=True):
        chat.persist_transcript_rows(s, [{"index": 50, "role": "assistant", "text": "c",
                                          "content": {"usage": {**u1, "message_id": "m3"}}}], attribute=False)
    typed = TurnUsage.objects.get(turn_ref=f"chat:{s.pk.hex}:40")
    assert (typed.output_tokens, typed.user, typed.turn) == (40, tree["ceo"], None)


def test_an_agents_login_and_a_system_account_are_not_people(tree):
    """Ada sending Hal work is Ada, not a person; nor is a CloudWatch sender."""
    from apps.workspaces.models import SystemAccount

    ada_login = _user("ada@dimagi-ai.com")
    Agent.objects.create(slug="ada", name="Ada", workspace_id="dimagi", user=ada_login)
    alarms = _user("alarms@system.canopy.invalid")
    SystemAccount.objects.create(user=alarms, workspace_id="dimagi", name="alarms")
    _turn(tree["hal"], "p1", ada_login)
    _turn(tree["eva"], "p2", alarms)
    _turn(tree["eva"], "p3", tree["ceo"])
    kinds = dict(Activity.objects.filter(kind=activity.TURN_ENQUEUED).values_list("actor__email", "actor_kind"))
    assert kinds == {"ada@dimagi-ai.com": "agent", "alarms@system.canopy.invalid": "system", "ceo@dimagi.com": "user"}
    body = _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=7").json()
    assert [u["email"] for u in body["users"]] == ["ceo@dimagi.com"] and body["totals"]["active_users"] == 1

    # Facts recorded before the login was known to be an agent's are fixed by the backfill.
    Activity.objects.update(actor_kind="user")
    assert activity.reclassify() == 2


def test_health_counts_projects_open_tasks_and_waits_like_the_agent_pages(tree):
    from apps.agents.models import AgentProject, AgentTask

    hal = tree["hal"]
    AgentProject.objects.create(agent=hal, ext_id="P1", name="Live")
    AgentProject.objects.create(agent=hal, ext_id="P2", name="Shelved", status=AgentProject.ARCHIVED)
    AgentTask.objects.create(agent=hal, ext_id="T1", title="open", status=AgentTask.IN_PROGRESS)
    AgentTask.objects.create(agent=hal, ext_id="T2", title="parked", status=AgentTask.SUGGESTED,
                             waiting_on_user=tree["builder"])
    AgentTask.objects.create(agent=hal, ext_id="T3", title="done", status=AgentTask.DONE,
                             waiting_on_user=tree["builder"])
    hal.interface = {"capabilities": {"ask": {"callers": ["member"]}}}
    hal.save(update_fields=["interface"])
    row = next(a for a in _get(tree["ceo"], "/api/workspaces/dimagi/health").json()["agents"] if a["slug"] == "hal")
    assert (row["projects"], row["open_tasks"], row["waiting_tasks"]) == (1, 2, 1)
    assert (row["has_interface"], row["capabilities"]) == (True, 1)


def test_health_shows_open_sessions_and_only_the_latest_ones_the_viewer_may_open(tree):
    from apps.canopy_sessions.models import Session

    hal = tree["hal"]
    Session.objects.create(workspace_id="connect", agent=hal, created_by=tree["builder"], title="builder's chat")
    Session.objects.create(workspace_id="connect", agent=hal, created_by=tree["builder"], title="old",
                           status=Session.ARCHIVED)
    body = _get(tree["builder"], "/api/workspaces/connect/health")
    # The builder is an editor of connect: health is the admin tier.
    assert body.status_code in (403, 404)
    row = next(a for a in _get(tree["ceo"], "/api/workspaces/dimagi/health").json()["agents"] if a["slug"] == "hal")
    assert row["active_sessions"] == 1 and row["last_session_at"] is not None
    from apps.canopy_sessions import access

    readable = set(access.readable_sessions(tree["ceo"]).filter(agent=hal).values_list("title", flat=True))
    assert {s["title"] for s in row["recent_sessions"]} == readable & {"builder's chat", "old"}
    assert all(s["active"] == (s["title"] == "builder's chat") for s in row["recent_sessions"])


# ---- fixing one field: the repository ----------------------------------------


def _patch(user, path, body):
    c = Client()
    c.force_login(user)
    return c.patch(path, data=json.dumps(body), content_type="application/json")


def test_the_repository_step_links_to_its_own_field_not_the_create_wizard(tree):
    muse = Agent.objects.create(slug="muse", name="Muse", workspace_id="dimagi")
    step = next(s for s in setup_steps(muse) if s["key"] == "repo")
    assert step["href"] == "/w/dimagi/agents/muse/settings#repository"
    assert step["action"] == "Set its repository" and "agents/new" not in step["href"]
    # And an ownerless agent's GitHub step says so, rather than "its owner, its owner".
    github = next(s for s in setup_steps(muse) if s["key"] == "github")
    assert "its owner, its owner" not in github["detail"] and github["href"].endswith("#operators")


def test_an_admin_sets_just_the_repository_and_it_is_audited(tree):
    from apps.events.models import SettingChange

    muse = Agent.objects.create(slug="muse", name="Muse", workspace_id="dimagi", owner=tree["ceo"])
    r = _patch(tree["ceo"], "/api/agents/muse/repository", {"repo_url": "git@github.com:dimagi/muse.git"})
    assert r.status_code == 200, r.content
    muse.refresh_from_db()
    assert muse.repo_url == "https://github.com/dimagi/muse"
    assert next(s for s in setup_steps(muse) if s["key"] == "repo")["status"] == "done"
    change = SettingChange.objects.get(operation="set_repository")
    assert change.changes[0]["after"]["repo_url"] == "https://github.com/dimagi/muse"


def test_only_its_admins_set_the_repository_and_only_to_github(tree):
    Agent.objects.create(slug="muse", name="Muse", workspace_id="dimagi", owner=tree["ceo"])
    assert _patch(tree["staff"], "/api/agents/muse/repository",
                  {"repo_url": "https://github.com/x/evil"}).status_code == 403
    assert _patch(tree["ceo"], "/api/agents/muse/repository",
                  {"repo_url": "https://gitlab.com/x/y"}).status_code == 422
