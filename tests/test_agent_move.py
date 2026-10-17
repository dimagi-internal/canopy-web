"""Moving an agent between workspaces carries its history (#1532).

Before this, the upsert's `workspace` field set `Agent.workspace` and nothing
else: the agent's sessions, mail, capability grants and agent-scoped rules all
carry their own `workspace` FK, so they stayed in the old tenant — invisible
from the new one, or 404 through the by-slug gates. Jonathan, 2026-10-16: "we
want to move the agent and its history which should all be possible and first
class. This would happen if someone transferred divisions."
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.admission.models import AdmissionRule
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.capabilities.models import CapabilityGrant
from apps.events.models import Event, SettingChange
from apps.harness.models import Turn
from apps.mail.models import MailDomain, MailDomainShare, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

User = get_user_model()


def _user(email):
    return User.objects.create_user(username=email, email=email, password="x")


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _member(ws, user, role):
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)


@pytest.fixture
def m():
    owner = _user("owner@dimagi.com")
    editor = _user("editor@dimagi.com")
    src_only = _user("src-only@dimagi.com")
    src = Workspace.objects.create(slug="src", display_name="Src", created_by=owner)
    dst = Workspace.objects.create(slug="dst", display_name="Dst", created_by=owner)
    _member(src, owner, WorkspaceMembership.OWNER)
    _member(dst, owner, WorkspaceMembership.EDITOR)
    _member(src, editor, WorkspaceMembership.EDITOR)
    _member(dst, editor, WorkspaceMembership.EDITOR)
    _member(src, src_only, WorkspaceMembership.EDITOR)

    agent = Agent.objects.create(slug="mover", name="Mover", workspace=src, owner=owner)
    other = Agent.objects.create(slug="stayer", name="Stayer", workspace=src, owner=owner)
    Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t1")
    # An older agent turn that carries its own workspace FK as well.
    Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t2", workspace=src)
    Turn.objects.create(agent=other, origin=Turn.ORIGIN_API, idempotency_key="t3")
    session = Session.objects.create(workspace=src, agent=agent, title="chat")
    SessionParticipant.objects.create(session=session, user=src_only)
    Session.objects.create(workspace=src, agent=other, title="not ours")

    own = MailDomain.objects.create(workspace=src, name="src.org", status="verified")
    shared = MailDomain.objects.create(workspace=src, name="shared.org", status="verified")
    MailDomainShare.objects.create(domain=shared, workspace=dst)
    stuck = MailRoute.objects.create(domain=own, local_part="mover", kind="agent", agent=agent)
    follows = MailRoute.objects.create(domain=shared, local_part="mover", kind="agent", agent=agent)
    thread = MailThread.objects.create(workspace=src, route=stuck, subject="Hello", message_count=1)
    MailMessage.objects.create(thread=thread, direction="in", message_id="<in-1@x.org>",
                               from_addr="x@x.org", subject="Hello", text="hi")

    CapabilityGrant.objects.create(workspace=src, agent=agent, capability_name="box")
    AdmissionRule.objects.create(workspace=src, agent=agent, origin="slack", reason="agent-scoped")
    AdmissionRule.objects.create(workspace=src, origin="slack", reason="workspace-wide")
    return type("M", (), dict(owner=owner, editor=editor, src_only=src_only, src=src, dst=dst,
                              agent=agent, session=session, thread=thread, stuck=stuck,
                              follows=follows))


def _move(user, slug="mover", ws="dst", dry_run=False):
    url = f"/api/agents/{slug}/move" + ("?dry_run=1" if dry_run else "")
    return _client(user).post(url, {"workspace": ws}, content_type="application/json")


def _rows(body) -> dict[str, int]:
    return {t["table"]: t["rows"] for t in body["tables"]}


def _where_history_is(m) -> set[str]:
    m.session.refresh_from_db()
    m.thread.refresh_from_db()
    return {m.session.workspace_id, m.thread.workspace_id,
            *MailMessage.objects.filter(thread=m.thread).values_list("workspace_id", flat=True),
            *CapabilityGrant.objects.filter(agent=m.agent).values_list("workspace_id", flat=True),
            *AdmissionRule.objects.filter(agent=m.agent).values_list("workspace_id", flat=True)}


def test_a_move_carries_turns_sessions_and_mail_and_they_read_back_from_the_destination(m):
    res = _move(m.owner)
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["moved"] is True and body["source"] == "src" and body["destination"] == "dst"

    m.agent.refresh_from_db()
    assert m.agent.workspace_id == "dst"
    assert _where_history_is(m) == {"dst"}
    # Nothing that is not this agent's moved.
    assert Session.objects.get(agent__slug="stayer").workspace_id == "src"
    assert AdmissionRule.objects.get(reason="workspace-wide").workspace_id == "src"
    assert set(Turn.objects.filter(agent=m.agent).values_list("workspace_id", flat=True)) <= {None, "dst"}

    owner = _client(m.owner)
    turns = owner.get("/api/w/dst/agents/mover/turns/")
    assert turns.status_code == 200, turns.content
    ours = {str(pk) for pk in Turn.objects.filter(agent=m.agent).values_list("pk", flat=True)}
    assert {t["id"] for t in turns.json()["items"]} == ours and len(ours) == 2
    threads = owner.get("/api/mail/threads/dst", {"agent": "mover"})
    assert [t["thread_id"] for t in threads.json()["items"]] == [str(m.thread.uuid)]
    assert owner.get(f"/api/mail/threads/dst/{m.thread.uuid}").status_code == 200
    assert owner.get(f"/api/mail/threads/src/{m.thread.uuid}").status_code == 404
    assert owner.get("/api/w/src/agents/mover/").status_code == 404


def test_the_dry_run_changes_nothing_and_matches_the_move(m):
    dry = _move(m.owner, dry_run=True)
    assert dry.status_code == 200, dry.content
    assert dry.json()["dry_run"] is True and dry.json()["moved"] is False
    m.agent.refresh_from_db()
    assert m.agent.workspace_id == "src"
    assert _where_history_is(m) == {"src"}
    assert not Event.objects.filter(source="agents.move").exists()
    assert not SettingChange.objects.filter(operation="move_agent").exists()

    real = _move(m.owner)
    assert real.status_code == 200, real.content
    assert _rows(dry.json()) == _rows(real.json())
    assert dry.json()["stops_working"] == real.json()["stops_working"]
    rows = _rows(real.json())
    assert rows["harness.Turn"] == 2
    assert rows["canopy_sessions.Session"] == 1
    assert rows["mail.MailThread"] == 1 and rows["mail.MailMessage"] == 1
    assert rows["mail.MailRoute"] == 1  # the shared-domain one; the other cannot follow
    assert rows["capabilities.CapabilityGrant"] == 1
    assert rows["admission.AdmissionRule"] == 1


def test_a_mail_route_on_a_source_owned_domain_is_reported_and_stops_routing(m):
    dry = _move(m.owner, dry_run=True).json()
    routes = [b for b in dry["stops_working"] if b["kind"] == "mail_route"]
    assert len(routes) == 1 and "mover@src.org" in routes[0]["detail"]
    assert "shared.org" not in routes[0]["detail"]

    assert _move(m.owner).status_code == 200
    m.stuck.refresh_from_db()
    m.follows.refresh_from_db()
    assert (m.stuck.workspace_id, m.stuck.enabled) == ("src", False)
    assert (m.follows.workspace_id, m.follows.enabled) == ("dst", True)


def test_a_non_admin_is_refused_and_nothing_moves(m):
    res = _move(m.editor)
    assert res.status_code == 403, res.content
    res = _move(m.editor, dry_run=True)
    assert res.status_code == 403, res.content
    m.agent.refresh_from_db()
    assert m.agent.workspace_id == "src"
    assert _where_history_is(m) == {"src"}


def test_a_destination_the_caller_is_not_in_is_a_404(m):
    nowhere = Workspace.objects.create(slug="nowhere", display_name="N", created_by=m.src_only)
    _member(nowhere, m.src_only, WorkspaceMembership.OWNER)
    assert _move(m.owner, ws="nowhere").status_code == 404
    assert _move(m.owner, ws="no-such").status_code == 404
    viewer_only = Workspace.objects.create(slug="looking", display_name="L", created_by=m.owner)
    _member(viewer_only, m.owner, WorkspaceMembership.VIEWER)
    assert _move(m.owner, ws="looking").status_code == 403
    m.agent.refresh_from_db()
    assert m.agent.workspace_id == "src"


def test_moving_to_where_it_already_is_is_refused(m):
    assert _move(m.owner, ws="src").status_code == 422


def test_the_move_is_recorded_in_both_workspaces(m):
    assert _move(m.owner).status_code == 200
    out = Event.objects.get(workspace=m.src, source="agents.move")
    inn = Event.objects.get(workspace=m.dst, source="agents.move")
    assert (out.kind, inn.kind) == ("agent.moved_out", "agent.moved_in")
    for e in (out, inn):
        assert e.payload["by"] == "owner@dimagi.com" and e.payload["agent"] == "mover"
        assert e.payload["counts"]["canopy_sessions.Session"] == 1
    change = SettingChange.objects.get(operation="move_agent")
    assert change.changes == [{"scope": "agent.workspace", "before": "src", "after": "dst"}]


def test_participants_outside_the_destination_lose_the_chat(m):
    dry = _move(m.owner, dry_run=True).json()
    assert any(b["kind"] == "chat_participants" for b in dry["stops_working"])
    assert _move(m.owner).status_code == 200
    assert not SessionParticipant.objects.filter(session=m.session, user=m.src_only).exists()


def test_a_collision_in_the_destination_moves_nothing(m):
    """Atomic: a row that cannot be re-homed rolls back the whole move."""
    there = MailThread.objects.create(workspace=m.dst, subject="dup", address="mover@src.org")
    MailMessage.objects.create(thread=there, direction="in", message_id="<in-1@x.org>",
                               mailbox="mover@src.org", from_addr="x@x.org")
    res = _move(m.owner)
    assert res.status_code == 409, res.content
    m.agent.refresh_from_db()
    assert m.agent.workspace_id == "src"
    assert _where_history_is(m) == {"src"}
    m.follows.refresh_from_db()
    assert m.follows.workspace_id == "src"
    assert not Event.objects.filter(source="agents.move").exists()


def test_the_upsert_workspace_field_is_the_same_full_move(m):
    """`canopy agent register --workspace` must not leave history behind."""
    res = _client(m.owner).post("/api/agents/", {"slug": "mover", "name": "Mover", "workspace": "dst"},
                                content_type="application/json")
    assert res.status_code == 201, res.content
    m.agent.refresh_from_db()
    assert m.agent.workspace_id == "dst"
    assert _where_history_is(m) == {"dst"}
    assert Event.objects.filter(workspace=m.dst, kind="agent.moved_in").exists()


def test_the_upsert_refuses_a_move_for_a_non_admin_and_writes_nothing(m):
    res = _client(m.editor).post("/api/agents/", {"slug": "mover", "name": "Renamed", "workspace": "dst"},
                                 content_type="application/json")
    assert res.status_code == 403, res.content
    m.agent.refresh_from_db()
    assert (m.agent.workspace_id, m.agent.name) == ("src", "Mover")
    assert _where_history_is(m) == {"src"}


def test_the_agents_own_login_joins_only_when_the_mover_may_add_members(m):
    login = _user("mover-agent@dimagi-ai.com")
    _member(m.src, login, WorkspaceMembership.EDITOR)
    Agent.objects.filter(pk=m.agent.pk).update(user=login)
    # The owner is only an editor in dst: the login cannot be added, and the
    # dry run says so.
    dry = _move(m.owner, dry_run=True).json()
    assert any(b["kind"] == "agent_login" for b in dry["stops_working"])
    WorkspaceMembership.objects.filter(workspace=m.dst, user=m.owner).update(
        role=WorkspaceMembership.OWNER)
    assert _move(m.owner).status_code == 200
    assert WorkspaceMembership.objects.filter(workspace=m.dst, user=login).exists()


def test_an_agent_session_points_at_the_agent_move(m):
    from apps.canopy_sessions import filing

    assert "/api/agents/mover/move" in filing.move_refusal(m.owner, m.session)


#: Tables tied to an agent AND carrying a workspace FK that a move deliberately
#: does not report, each with its reason. Everything else such must appear in
#: the plan, moved or left behind.
_NOT_THE_AGENTS = {
    "contacts.PersonAccess": "a person's audit of who read them stays where it happened",
    "harness.FailureInvestigation": "the DEBUGGER agent's workspace; `agents` only lists who failed",
    "shareouts.Shareout": "a workspace's briefing, produced by the agent for that workspace",
    "beta_requests.BetaRequest": "`user_agent` is a browser string, not an agent",
    "events.SettingChange": "`user_agent` is a browser string; audit stays where it happened",
    "agents.EngagementEvent": "what people did, recorded in the workspace they did it in; telemetry stays there",
}


def test_every_agent_scoped_table_with_its_own_workspace_is_in_the_move(m):
    """A new table with an agent and its own workspace FK must join the move
    (`apps/agents/move.py::register`) or say here why not — else every moved
    agent silently leaves those rows behind, the bug #1532 fixed."""
    from django.apps import apps

    from apps.agents import move

    def agent_scoped(model):
        fields = model._meta.concrete_fields
        has_ws = any(f.is_relation and f.related_model is Workspace for f in fields)
        has_agent = any((f.is_relation and f.related_model is Agent)
                        or (not f.is_relation and "agent" in f.name) for f in fields)
        return has_ws and has_agent

    p = move.plan(m.agent, m.dst, by=m.owner)
    reported = {t["table"] for t in p.tables} | {"contacts.PersonFact", "contacts.PersonGrant"}
    missing = sorted(model._meta.label for model in apps.get_models()
                     if agent_scoped(model) and model._meta.label not in reported
                     and model._meta.label not in _NOT_THE_AGENTS)
    assert not missing, f"agent-scoped tables the move does not handle: {missing}"
