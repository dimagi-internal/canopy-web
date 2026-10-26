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

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import access, attach
from apps.canopy_sessions.consumers import SessionConsumer
from apps.canopy_sessions.models import SessionParticipant
from apps.workspaces.models import WorkspaceMembership
from tests._source import iter_app_sources
from tests.fixtures.session_world import make_member, make_user, session_world

pytestmark = pytest.mark.django_db(transaction=True)



_user = make_user
_member = make_member
_world = session_world


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


def _socket_pulls(user, session):
    """Whether opening the chat socket asked the runner to stream (the attach
    count went up while connected), and whether closing it put the count back."""
    from channels.db import database_sync_to_async

    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope["user"] = user
        comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
        before = await database_sync_to_async(attach.count)(session.id)
        connected, _ = await comm.connect()
        await comm.receive_json_from()   # the snapshot: sent once the attach decision is made
        during = await database_sync_to_async(attach.count)(session.id)
        await comm.disconnect()
        after = await database_sync_to_async(attach.count)(session.id)
        return connected, during - before, after - before
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


def test_rest_and_the_socket_agree_on_who_may_pull_from_a_machine():
    """Asking a runner for something — live streaming (`attach`, REST or the
    socket) or history (`backfill`) — is `access.may_pull`, at every door. Nobody
    else's open tab makes canopy reach down to a laptop."""
    people, shapes, _ws = _world()
    for who, user in people.items():
        c = _client(user)
        for label, s in shapes.items():
            if not access.can_read(user, s):
                continue
            pulls = access.may_pull(user, s)
            before = attach.count(s.id)
            c.post(f"/api/canopy-sessions/{s.id}/attach")
            assert (attach.count(s.id) - before == 1) is pulls, f"REST attach: {who}/{label}"
            c.post(f"/api/canopy-sessions/{s.id}/detach")
            assert attach.count(s.id) == before, f"REST detach unbalanced: {who}/{label}"
            backfill = c.post(f"/api/canopy-sessions/{s.id}/backfill")
            assert (backfill.status_code != 403) is pulls, f"backfill: {who}/{label}"
            if access.role_for(user, s) is not None and _socket_role(user, s):
                connected, during, after = _socket_pulls(user, s)
                assert connected
                assert (during == 1) is pulls, f"socket attach: {who}/{label}"
                assert after == 0, f"socket detach unbalanced: {who}/{label}"


def test_a_laptop_session_is_its_box_owner_s_alone():
    """A runner-discovered session belongs to the person whose machine it runs
    on — not the workspace, not its owner, not any agent login."""
    people, s, _ws = _world()
    laptop = s["mates_laptop"]
    assert access.role_for(people["mate"], laptop) == SessionParticipant.OWNER
    assert access.may_pull(people["mate"], laptop)
    for who in ("owner", "viewer", "box_admin", "hal_login", "coach", "outsider"):
        assert access.can_read(people[who], laptop) is False, who
    # The owner adds a colleague explicitly: now they read, write and may pull.
    resp = _client(people["mate"]).post(
        f"/api/canopy-sessions/{laptop.id}/participants",
        {"email": "viewer@dimagi.com", "role": "editor"}, content_type="application/json")
    assert resp.status_code == 200
    assert access.can_write(people["viewer"], laptop) and access.may_pull(people["viewer"], laptop)
    # A session no runner reports yet is nobody's (no box owner to belong to).
    assert all(not access.can_read(u, s["unbound_discovered"]) for u in people.values())


def test_a_coach_and_a_runner_admin_read_what_was_sent_and_nothing_more():
    people, s, _ws = _world()
    for who in ("coach", "box_admin"):
        user = people[who]
        assert access.role_for(user, s["discovered"]) == SessionParticipant.VIEWER, who
        assert access.can_write(user, s["discovered"]) is False, who
        assert access.may_pull(user, s["discovered"]) is False, who
        r = _client(user).post(f"/api/canopy-sessions/{s['discovered'].id}/backfill")
        assert r.status_code == 403 and "already sent" in r.json()["detail"], who
    # The runner admin may move what is on the box they were trusted with...
    assert access.may_move(people["box_admin"], s["discovered"])
    # ...the coach may not, and neither sees the web chats a review grant does not cover.
    assert access.may_move(people["coach"], s["discovered"]) is False
    assert access.can_read(people["box_admin"], s["own_web"]) is False
    assert access.can_read(people["coach"], s["agent_thread"]) is False
    assert access.can_read(people["hal_login"], s["discovered"]) is False
    # An agent's own login reads its own work, read-only, and pulls nothing.
    assert access.role_for(people["hal_login"], s["agent_thread"]) == SessionParticipant.VIEWER
    assert access.may_pull(people["hal_login"], s["agent_thread"]) is False
    assert access.can_read(people["coach"], s["agent_thread"]) is False   # another agent's


def test_the_rule_itself():
    people, s, _ws = _world()
    owner, mate, viewer, outsider = (people[k] for k in ("owner", "mate", "viewer", "outsider"))
    assert access.role_for(owner, s["own_web"]) == SessionParticipant.OWNER
    assert access.role_for(mate, s["own_web"]) is None            # a co-tenant is not in it
    assert access.role_for(mate, s["bound_web"]) is None          # nor once a runner picks it up
    assert access.role_for(viewer, s["shared_web"]) == SessionParticipant.VIEWER
    assert access.role_for(mate, s["discovered"]) is None         # someone else's laptop
    assert access.role_for(owner, s["discovered"]) == SessionParticipant.OWNER  # your laptop
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
# read in Slack, a member on an email thread its proven sender addressed to
# them. Adding a caller here is adding a way into other people's
# conversations — say why in the module you add it to.
ALLOWED_GRANTERS = {
    "apps/canopy_sessions/participants.py",  # ensure_participant itself
    "apps/canopy_sessions/services.py",      # create_session: the creator as owner
    "apps/canopy_sessions/api.py",           # add_participant: the owner shares
    "apps/slack/audience.py",                # a member in the Slack conversation
    "apps/harness/channels/email.py",        # a member on an (aligned) email thread
}


def test_only_explicit_acts_grant_participation():
    offenders = []
    for src in iter_app_sources(("apps",), exclude=ALLOWED_GRANTERS):
        rel = src.rel
        for node in ast.walk(src.tree):
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
    # ...nor the box owner's of their own laptop session...
    SessionParticipant.objects.create(session=s["discovered"], user=people["owner"],
                                      role=SessionParticipant.VIEWER)
    assert access.role_for(people["owner"], s["discovered"]) == SessionParticipant.OWNER
    # ...while a row still RAISES: the coach, a viewer by its leg, made an editor is one.
    SessionParticipant.objects.create(session=s["discovered"], user=people["coach"],
                                      role=SessionParticipant.EDITOR)
    assert access.role_for(people["coach"], s["discovered"]) == SessionParticipant.EDITOR


def test_an_agent_thread_belongs_to_whoever_runs_the_agent():
    """On labs every agent's `owner` is null, so "the agent's owner" matched
    nobody and Hal's alarm thread was readable only through an old auto-join
    row. Leg 4 is `Agent.is_admin`: the owner, a workspace owner, an explicit
    AgentAdmin. A plain co-tenant is still out."""
    from apps.agents.models import AgentAdmin

    people, s, ws = _world()
    thread = s["agent_thread"]
    Agent.objects.filter(pk=thread.agent_id).update(owner=None)
    thread.refresh_from_db()
    admin = _user("admin")
    _member(admin, ws)
    AgentAdmin.objects.create(agent=thread.agent, user=admin)

    for user in (people["owner"], admin, people["mate"], people["outsider"]):
        agent = Agent.objects.get(pk=thread.agent_id)
        # The ACL and the agent's own definition of "runs it" never disagree.
        assert access.can_read(user, thread) is agent.is_admin(user), user.username
    assert access.role_for(people["owner"], thread) == SessionParticipant.OWNER  # workspace owner
    assert access.role_for(admin, thread) == SessionParticipant.OWNER
    assert access.role_for(people["mate"], thread) is None
