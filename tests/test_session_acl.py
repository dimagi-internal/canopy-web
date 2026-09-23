"""The chat-session ACL, pinned end to end. The rule lives in
`apps/canopy_sessions/access.py`; this file makes every surface agree with it.

It exists because three surfaces used to answer "may this person see this
chat?" three ways. REST used `visible_session_q`, the socket admitted any
workspace member and auto-joined them as an editor, and attachments checked the
tenant alone. The visible symptom was a push notification that opened on a 404
and then worked on reload (2026-09-23). The real bug was that one socket
connection made a private conversation permanently yours.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.consumers import SessionConsumer
from apps.canopy_sessions.models import RunnerBinding, Session, SessionParticipant
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _member(user, ws, role=WorkspaceMembership.EDITOR):
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=role)


def _world():
    owner, mate, viewer, outsider = _user("owner"), _user("mate"), _user("viewer"), _user("outsider")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    _member(owner, ws, WorkspaceMembership.OWNER)
    _member(mate, ws)
    _member(viewer, ws)
    agent = Agent.objects.create(slug="hal", name="Hal", owner=owner, workspace=ws)
    runner = Runner.objects.create(name="r", kind=Runner.EMDASH, host="h", paired_by=owner,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now())

    def bound(session, key):
        RunnerBinding.objects.create(session=session, runner=runner, session_key=key,
                                     live_seen_at=timezone.now())
        return session

    shapes = {
        "own_web": Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB),
        "shared_web": Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB),
        "discovered": bound(Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER), "d"),
        "agent_thread": Session.objects.create(workspace=ws, agent=agent, origin=Session.ORIGIN_RUNNER),
        "bound_web": bound(Session.objects.create(workspace=ws, created_by=owner,
                                                  origin=Session.ORIGIN_WEB), "b"),
    }
    SessionParticipant.objects.create(session=shapes["own_web"], user=owner, role=SessionParticipant.OWNER)
    SessionParticipant.objects.create(session=shapes["shared_web"], user=viewer,
                                      role=SessionParticipant.VIEWER)
    return {"owner": owner, "mate": mate, "viewer": viewer, "outsider": outsider}, shapes, ws


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _socket_role(user, session):
    """What the chat socket grants: connects or not, and whether it writes a row."""
    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope["user"] = user
        comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
        connected, _ = await comm.connect()
        await comm.disconnect()
        return connected
    return async_to_sync(go)()


def test_rest_the_socket_and_the_list_agree_for_every_shape_and_person():
    """ONE answer per (person, session), whichever door they knock on."""
    people, shapes, _ws = _world()
    for who, user in people.items():
        c = _client(user)
        listed = {r["id"] for r in c.get("/api/canopy-sessions/?state=all").json()}
        for label, s in shapes.items():
            rows_before = SessionParticipant.objects.filter(session=s, user=user).count()
            rest = c.get(f"/api/canopy-sessions/{s.id}").status_code == 200
            sock = _socket_role(user, s)
            expected = access.can_read(user, s)
            assert rest is expected, f"REST disagrees on {who}/{label}"
            assert sock is expected, f"socket disagrees on {who}/{label}"
            assert (str(s.id) in listed) is expected, f"list disagrees on {who}/{label}"
            assert SessionParticipant.objects.filter(session=s, user=user).count() == rows_before, \
                f"opening {label} as {who} wrote a participant row"


def test_the_rule_itself():
    people, s, _ws = _world()
    owner, mate, viewer, outsider = (people[k] for k in ("owner", "mate", "viewer", "outsider"))
    assert access.role_for(owner, s["own_web"]) == SessionParticipant.OWNER
    assert access.role_for(mate, s["own_web"]) is None            # a co-tenant is not in it
    assert access.role_for(mate, s["bound_web"]) is None          # nor once a runner picks it up
    assert access.role_for(viewer, s["shared_web"]) == SessionParticipant.VIEWER
    assert access.role_for(mate, s["discovered"]) == SessionParticipant.EDITOR  # tenant-visible
    assert access.role_for(owner, s["agent_thread"]) == SessionParticipant.OWNER  # your agent's
    assert access.role_for(mate, s["agent_thread"]) is None
    assert all(access.role_for(outsider, x) is None for x in s.values())


def test_a_viewer_reads_but_cannot_act():
    people, s, _ws = _world()
    c = _client(people["viewer"])
    sid = s["shared_web"].id
    assert c.get(f"/api/canopy-sessions/{sid}").status_code == 200
    assert c.post(f"/api/canopy-sessions/{sid}/send", {"text": "hi"},
                  content_type="application/json").status_code == 403
    assert c.post(f"/api/canopy-sessions/{sid}/archive").status_code == 403


def test_leaving_the_workspace_takes_the_chat_with_it():
    """Tenant first, always. The socket used to honour a participant row after
    its holder was off-boarded; REST did not."""
    people, s, ws = _world()
    WorkspaceMembership.objects.filter(user=people["viewer"], workspace=ws).delete()
    assert access.can_read(people["viewer"], s["shared_web"]) is False
    assert _socket_role(people["viewer"], s["shared_web"]) is False


def test_the_owner_shares_and_only_the_owner():
    people, s, _ws = _world()
    sid = s["own_web"].id
    add = lambda who, email, role="editor": _client(people[who]).post(  # noqa: E731
        f"/api/canopy-sessions/{sid}/participants", {"email": email, "role": role},
        content_type="application/json")

    assert add("mate", "viewer@dimagi.com").status_code == 404          # cannot even see it
    assert add("owner", "outsider@dimagi.com").status_code == 404       # not in the workspace
    resp = add("owner", "MATE@dimagi.com", "viewer")
    assert resp.status_code == 200
    assert {p["email"]: p["role"] for p in resp.json()}["mate@dimagi.com"] == "viewer"
    assert access.role_for(people["mate"], s["own_web"]) == SessionParticipant.VIEWER
    assert add("mate", "viewer@dimagi.com").status_code == 403          # a viewer cannot share

    mate_id = people["mate"].pk
    # Anyone can leave; the owner cannot be removed.
    assert _client(people["mate"]).delete(
        f"/api/canopy-sessions/{sid}/participants/{mate_id}").status_code == 200
    assert access.can_read(people["mate"], s["own_web"]) is False
    assert _client(people["owner"]).delete(
        f"/api/canopy-sessions/{sid}/participants/{people['owner'].pk}").status_code == 409


def test_the_agent_owner_can_share_the_agent_s_thread():
    people, s, _ws = _world()
    resp = _client(people["owner"]).post(
        f"/api/canopy-sessions/{s['agent_thread'].id}/participants",
        {"email": "mate@dimagi.com"}, content_type="application/json")
    assert resp.status_code == 200
    assert access.role_for(people["mate"], s["agent_thread"]) == SessionParticipant.EDITOR


def test_bulk_reset_only_touches_sessions_you_may_act_in():
    people, s, _ws = _world()
    out = _client(people["mate"]).post("/api/canopy-sessions/reset", {"dry_run": True},
                                       content_type="application/json").json()
    touched = {r["session_id"] for r in out["reset"] + out["skipped"]}
    assert str(s["own_web"].id) not in touched
    assert str(s["bound_web"].id) not in touched


# Who may create a SessionParticipant row. Each is an EXPLICIT act: creating a
# chat, the owner sharing it, a member joining a Slack thread they can already
# read in Slack. Adding a caller here is adding a way into other people's
# conversations — say why in the module you add it to.
ALLOWED_GRANTERS = {
    "apps/canopy_sessions/participants.py",  # ensure_participant itself
    "apps/canopy_sessions/services.py",      # create_session: the creator as owner
    "apps/canopy_sessions/api.py",           # add_participant: the owner shares
    "apps/slack/services.py",                # a member in a Slack thread
}


def test_only_explicit_acts_grant_participation():
    offenders = []
    for path in (ROOT / "apps").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/migrations/" in rel or "/tests/" in rel or rel in ALLOWED_GRANTERS:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name == "ensure_participant":
                offenders.append(f"{rel}:{node.lineno}")
            if name in ("create", "get_or_create", "update_or_create", "bulk_create") and \
                    isinstance(f, ast.Attribute) and "SessionParticipant" in ast.unparse(f.value):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"participant grants outside the allowlist: {offenders}"


def test_a_participant_row_never_demotes_what_the_rule_gives():
    """An old auto-join row said "editor" for Hal's owner on Hal's own thread,
    which hid the People control from the one person who could share it."""
    people, s, _ws = _world()
    SessionParticipant.objects.create(session=s["agent_thread"], user=people["owner"],
                                      role=SessionParticipant.VIEWER)
    assert access.role_for(people["owner"], s["agent_thread"]) == SessionParticipant.OWNER
    # ...while a row still RAISES: made an editor of a discovered session stays one.
    SessionParticipant.objects.create(session=s["discovered"], user=people["mate"],
                                      role=SessionParticipant.VIEWER)
    assert access.role_for(people["mate"], s["discovered"]) == SessionParticipant.EDITOR
