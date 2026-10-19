"""Workspace health + analytics: one answer for "set up right", one fact log for usage.

GET /api/workspaces/{slug}/health and /analytics read the tree the topology views
read, and compose the answers that already exist — the setup checklist, the vault
verdict, the analytics fact log — rather than keeping second opinions.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent, AgentCredential
from apps.agents.setup import setup_steps
from apps.agents.vault_health import vault_health
from apps.events import activity, usage
from apps.events.models import Activity, SessionUsage
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
    Turn.objects.filter(pk=t1.pk).update(session_key="claude-a1")
    usage.record(_box(tree), [_report("claude-a1", "hal", inp=300)])

    body = _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=7").json()
    assert body["totals"]["turns"] == 3
    assert body["totals"]["active_users"] == 2
    assert body["totals"]["input_tokens"] == 300 and "cost_usd" not in body["totals"]
    assert body["totals"]["sessions_with_usage"] == 1
    assert len(body["series"]) == 7 and body["series"][-1]["turns"] == 3
    users = {u["email"]: u for u in body["users"]}
    assert users["b@dimagi.com"]["turns"] == 1 and users["b@dimagi.com"]["input_tokens"] == 300
    assert users["b@dimagi.com"]["top"] == ["hal"]
    agents = {a["agent"]: a for a in body["agents"]}
    assert agents["eva"]["turns"] == 2 and agents["hal"]["input_tokens"] == 300

    only_root = _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=7&tree=false").json()
    assert only_root["totals"]["turns"] == 2


def test_backfill_seeds_facts_once(tree):
    from django.core.management import call_command

    turn = _turn(tree["hal"], "b1", tree["builder"])
    Activity.objects.all().delete()
    call_command("backfill_activity")
    call_command("backfill_activity")
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
    assert (row["has_interface"], row["capabilities"], row["full_access"]) == (True, 1, [])


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


# ---- repositories: work no agent runs -----------------------------------------


def _repo_session(tree, project="canopy-web"):
    from apps.canopy_sessions.models import Session

    return Session.objects.create(workspace_id="dimagi", project=project, created_by=tree["ceo"],
                                  origin=Session.ORIGIN_RUNNER, title="work")


def test_every_session_creation_emits_its_fact_once_with_its_repository(tree):
    s = _repo_session(tree)  # a raw create, as the runner's report sweep does
    facts = Activity.objects.filter(kind=activity.CHAT_SESSION_CREATED, ref=str(s.pk))
    assert list(facts.values_list("project", "agent_slug", "actor__email")) == [("canopy-web", "", "ceo@dimagi.com")]
    turn = _turn(tree["hal"], "once", tree["builder"])
    assert Activity.objects.filter(kind=activity.TURN_ENQUEUED, ref=str(turn.pk)).count() == 1


def test_analytics_has_a_row_per_repository(tree):

    from apps.canopy_sessions.models import RunnerBinding

    box = _box(tree)
    s = _repo_session(tree)
    RunnerBinding.objects.create(session=s, runner=box, session_key="t", transcript_id="claude-r1", host="h")
    usage.record(box, [_report("claude-r1", "canopy-web", cache=3)])
    body = _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=7").json()
    repo = next(r for r in body["repositories"] if r["repository"] == "canopy-web")
    assert (repo["sessions"], repo["cache_read_tokens"]) == (1, 3)
    assert body["totals"]["active_repositories"] == 1
    assert next(u for u in body["users"] if u["email"] == "ceo@dimagi.com")["top"] == ["canopy-web"]


# ---- runners on health --------------------------------------------------------


def test_health_lists_the_trees_runners_with_their_code_and_problems(tree, settings):
    from django.utils import timezone

    from apps.harness.models import Runner, RunnerAssignment
    from apps.workspaces.testing import enrolled

    settings.RUNNER_CODE_SHA = "new"
    box = enrolled(Runner.objects.create(
        name="laptop", kind=Runner.EMDASH, workspace_id="dimagi", owner=tree["ceo"], status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), code_sha="old",
        health={"checks": [{"name": "disk", "status": "warn", "detail": "90% full"},
                           {"name": "cdp", "status": "ok"}]}))
    RunnerAssignment.objects.create(agent=tree["eva"], runner=box, rank=0)
    body = _get(tree["ceo"], "/api/workspaces/dimagi/health").json()
    row = next(r for r in body["runners"] if r["name"] == "laptop")
    assert (row["code_current"], row["agent_count"]) == (False, 1)
    assert row["problems"] == [{"name": "disk", "status": "warn", "detail": "90% full"}]
    assert body["totals"]["runners"] >= 1
    assert row["agents"] == [{"slug": "eva", "name": tree["eva"].name, "workspace": tree["eva"].workspace_id}]
    assert (row["code_sha"], row["expected_code_sha"]) == ("old", "new")


def test_health_and_topology_serve_one_runner_row_with_its_reason_and_fix(tree):
    """Health and Topology → Runners render one table over one row: the box's own
    note, the fix for a known cause, and its usage report."""
    from django.utils import timezone

    from apps.harness.models import Runner
    from apps.workspaces.testing import enrolled

    enrolled(Runner.objects.create(
        name="laptop", kind=Runner.EMDASH, workspace_id="dimagi", owner=tree["ceo"], status=Runner.DEGRADED,
        last_heartbeat_at=timezone.now(), ready=False, status_note="emdash CDP unreachable on :9223 — not claiming",
        ready_note="emdash CDP unreachable on :9223"))
    health = next(r for r in _get(tree["ceo"], "/api/workspaces/dimagi/health").json()["runners"] if r["name"] == "laptop")
    topo = next(r for r in _get(tree["ceo"], "/api/workspaces/dimagi/runner-topology").json()["runners"]
                if r["name"] == "laptop")
    assert health == topo
    assert health["status"] == "degraded" and "Emdash CDP" in health["remedy"]
    assert health["can_administer"] is True and health["missing_sessions"] == 0


# ---- the box's usage report and missing sessions ------------------------------


def _box(tree, name="laptop"):
    from django.utils import timezone

    from apps.harness.models import Runner

    return Runner.objects.get_or_create(name=name, defaults=dict(
        kind=Runner.EMDASH, workspace_id="dimagi", owner=tree["ceo"], status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), host=name))[0]


def _report(cs, repo, *, day=None, inp=1, out=2, cache=3, title="Fix the login"):
    from django.utils import timezone

    day = day or str(timezone.now().date())
    return {"claude_session": cs, "repo": repo, "title": title, "first_at": f"{day}T09:00:00Z",
            "last_at": f"{day}T10:00:00Z", "days": [{"day": day, "input_tokens": inp, "output_tokens": out,
                                                     "cache_read_tokens": cache, "messages": 4, "model": "m"}]}


def test_a_report_is_matched_to_the_turn_that_ran_it_and_replaces_itself(tree):
    turn = _turn(tree["hal"], "r1", tree["builder"])
    Turn.objects.filter(pk=turn.pk).update(session_key="claude-x")
    box = _box(tree)
    assert usage.record(box, [_report("claude-x", "hal", out=10)]) == {"stored": 1, "matched": 1}
    usage.record(box, [_report("claude-x", "hal", out=12)])  # the session grew: replaced, not added
    row = SessionUsage.objects.get()
    assert (row.workspace_id, row.agent_slug, row.user, row.output_tokens) == ("connect", "hal", tree["builder"], 12)


def test_an_unknown_session_is_missing_until_its_admin_sends_it_up(tree, settings):
    box = _box(tree)
    usage.record(box, [_report("claude-lost", "canopy-web")])
    body = _get(tree["ceo"], "/api/workspaces/dimagi/analytics?days=7").json()
    assert body["totals"]["sessions_with_usage"] == 0  # not counted anywhere until it has a home
    lst = _get(tree["ceo"], f"/api/harness/runners/{box.pk}/missing-sessions").json()
    assert [(m["claude_session"], m["repo"], m["files_in"]) for m in lst["sessions"]] == [
        ("claude-lost", "canopy-web", "dimagi")]
    assert _get(tree["staff"], f"/api/harness/runners/{box.pk}/missing-sessions").status_code in (403, 404)

    c = Client()
    c.force_login(tree["ceo"])
    r = c.post(f"/api/harness/runners/{box.pk}/missing-sessions", data=json.dumps({"claude_sessions": ["claude-lost"]}),
               content_type="application/json")
    assert r.status_code == 200, r.content
    from apps.canopy_sessions.models import RunnerBinding, Session

    s = Session.objects.get(pk=r.json()["sessions"][0])
    b = RunnerBinding.objects.get(session=s)
    assert (s.project, s.workspace_id, b.session_key, b.transcript_id, b.backfill_requested) == (
        "canopy-web", "dimagi", "claude:claude-lost", "claude-lost", True)
    assert SessionUsage.objects.get().workspace_id == "dimagi"
    assert _get(tree["ceo"], f"/api/harness/runners/{box.pk}/missing-sessions").json()["sessions"] == []


def test_checking_for_missing_sessions_asks_the_box_for_a_full_rescan(tree):
    box = _box(tree)
    c = Client()
    c.force_login(tree["ceo"])
    assert c.post(f"/api/harness/runners/{box.pk}/usage-scan").status_code == 200
    box.refresh_from_db()
    assert box.usage_scan_requested_at is not None
    usage.record(box, [], full=True)
    box.refresh_from_db()
    assert box.usage_scan_requested_at is None and box.usage_scanned_at is not None


def test_health_shows_a_boxes_missing_count_and_who_may_send_them(tree):
    from apps.harness.models import RunnerAssignment

    box = _box(tree)
    RunnerAssignment.objects.create(agent=tree["eva"], runner=box, rank=0)
    usage.record(box, [_report("claude-lost", "canopy-web")])
    row = next(r for r in _get(tree["ceo"], "/api/workspaces/dimagi/health").json()["runners"] if r["name"] == "laptop")
    assert (row["missing_sessions"], row["can_administer"]) == (1, True)
    assert row["usage_reported_at"] is not None


def test_a_box_scans_only_the_repos_canopy_knows_for_it(tree):
    from apps.harness.models import RunnerAssignment

    box = _box(tree)
    box.capabilities = {"projects": ["canopy-web"]}
    box.save(update_fields=["capabilities"])
    Agent.objects.filter(pk=tree["eva"].pk).update(repo_url="https://github.com/dimagi/eva-agent")
    RunnerAssignment.objects.create(agent=tree["eva"], runner=box, rank=0)
    assert usage.scope(box) == ["canopy-web", "eva", "eva-agent"]


def test_health_shows_how_an_agent_is_used_and_where_it_ran(tree):
    """Setup is not the whole story: an agent with steps left can still be in use.
    Each row says who used it, how its turns ended and which runner ran them."""
    from apps.harness.models import Runner

    box = Runner.objects.create(name="cloud-1", kind="cloud", owner=tree["builder"])
    done, failed = _turn(tree["hal"], "u1", tree["builder"]), _turn(tree["hal"], "u2", tree["staff"])
    for turn, status in ((done, Turn.DONE), (failed, Turn.FAILED)):
        Turn.objects.filter(pk=turn.pk).update(claimed_by=box)
        turn.refresh_from_db()
        turn.status = status
        activity.for_turn(activity.TURN_FINISHED, turn)
    body = _get(tree["ceo"], "/api/workspaces/dimagi/health").json()
    hal = {a["slug"]: a for a in body["agents"]}["hal"]
    assert (hal["turns_30d"], hal["done_30d"], hal["failed_30d"], hal["people_30d"]) == (2, 1, 1, 2)
    assert hal["ran_on"] == [{"name": "cloud-1", "kind": "cloud", "turns": 2}]
    assert hal["left"] > 0 and body["totals"]["used_30d"] == 1


# ---- zero-knowledge agents' vault step, at the cost of a tree -----------------


def _keyset(user):
    from apps.passkeys.models import UserVaultKeyset

    return UserVaultKeyset.objects.create(user=user, public_jwk={"kty": "EC", "crv": "P-256", "x": "x", "y": "y"})


def _health_queries(user) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        assert _get(user, "/api/workspaces/dimagi/health").status_code == 200
    return len(ctx.captured_queries)


def test_health_does_not_ask_about_every_vault_key_in_the_system(tree):
    """The vault step asks, per ZK agent, whether anyone can open its vault. Asking
    `is_admin` of every keyset holder in the system made the health page's queries grow
    with keysets × agents; the cheap check is two queries per agent whatever the count."""
    for slug in ("zk1", "zk2", "zk3"):
        Agent.objects.create(slug=slug, name=slug, workspace_id="connect", owner=tree["builder"],
                             credential_source=Agent.CANOPY_WEB, zk_enabled=True, zk_kv=0)
    before = _health_queries(tree["ceo"])
    for i in range(8):  # vault keys held by people who administer none of these agents
        _keyset(_user(f"stranger{i}@dimagi.com"))
    assert _health_queries(tree["ceo"]) == before


def test_has_recipients_matches_recipients_for(tree):
    from apps.agents import zk_seal

    agent = Agent.objects.create(slug="zk", name="zk", workspace_id="connect", owner=tree["builder"],
                                 credential_source=Agent.CANOPY_WEB, zk_enabled=True, zk_kv=0)
    _keyset(tree["staff"])  # an editor of the parent: not an admin of the agent
    assert zk_seal.has_recipients(agent) is False and zk_seal.recipients_for(agent) == []
    _keyset(tree["ceo"])  # owner of the parent workspace: an admin by inheritance
    assert zk_seal.has_recipients(agent) is True and len(zk_seal.recipients_for(agent)) == 1


def test_admin_user_ids_agrees_with_is_admin(tree):
    from apps.agents.models import AgentAdmin

    agent = tree["hal"]
    AgentAdmin.objects.create(agent=agent, user=tree["staff"])
    outsider = _user("out@elsewhere.org")
    ids = agent.admin_user_ids()
    for u in (tree["ceo"], tree["staff"], tree["builder"], outsider):
        assert (u.pk in ids) == agent.is_admin(u), u.email
