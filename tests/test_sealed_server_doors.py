"""The server's doors to a sensitive (private) conversation, each driven through its
real entry point: who may open its socket, send into it, fork it, read its page,
ship its rows, join it, and what its info, push and key read say.

docs/architecture/tenancy.md § Sensitive conversations. One test (or two) per door
an adversarial audit found open; `tests/test_sensitive_read_doors.py` holds the
read doors that were already shut.
"""
from __future__ import annotations

import datetime as dt

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import AgentAdmin
from apps.canopy_sessions import page_state, services
from apps.canopy_sessions.models import (
    JoinRequest,
    Message,
    RunnerBinding,
    Session,
    SessionKeyWrap,
    SessionParticipant,
)
from apps.harness import sensitivity
from apps.harness.models import Turn, TurnEvent
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.sensitive import (
    ASKED,
    REPLY,
    ROW,
    SENSITIVE,
    _converse,
    _turn,
    _unowned_thread,
    mcp_as,
    minted,
    open_sealed,
    vault_key,
    world,
)

PHI = {"filters": {"patient": "Amina Okafor"}, "visible_ids": ["c-1"]}


@pytest.fixture()
def w(db):
    return world()


def _member(w, name, role=WorkspaceMembership.EDITOR, *, key=True):
    user = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=w["ws"], role=role)
    if key:
        vault_key(user)
    return user


def _browser(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _admin(w, name="ada"):
    """An admin of the conversation's agent who is NOT in it."""
    admin = _member(w, name)
    AgentAdmin.objects.create(agent=w["agent"], user=admin)
    return admin


# --- 1. a session minted from a token never opens the chat socket ------------------


def _connect(path: str, cookie: str):
    """Open `path` through the real socket stack (`RealtimeAuthMiddleware` → the chat
    routes) with a session cookie, as a browser or a token holder's script would.
    Returns (connected, frames)."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.routing import websocket_urlpatterns
    from apps.realtime.channels_auth import RealtimeAuthMiddleware

    app = RealtimeAuthMiddleware(URLRouter(websocket_urlpatterns))

    async def go():
        comm = WebsocketCommunicator(app, path, headers=[
            (b"cookie", f"{settings.SESSION_COOKIE_NAME}={cookie}".encode()),
            (b"origin", b"http://testserver")])
        ok, _ = await comm.connect()
        frames = []
        if ok:
            try:
                frames.append(await comm.receive_json_from(timeout=2))
            except Exception:  # noqa: BLE001 — no more frames
                pass
        try:
            await comm.disconnect()
        except BaseException:  # noqa: BLE001
            pass
        return ok, frames

    return async_to_sync(go)()


@pytest.mark.django_db(transaction=True)
def test_a_minted_session_cookie_cannot_open_a_sensitive_chat_socket():
    w = world()
    _converse(w["hot"])
    path = f"/ws/canopy-sessions/{w['hot'].id}/"
    cookie = minted(w["user"]).cookies[settings.SESSION_COOKIE_NAME].value
    ok, frames = _connect(path, cookie)
    assert not ok and frames == []
    # The person, in their own browser, still does: the door is shut to the token only.
    ok, frames = _connect(path, w["c"].cookies[settings.SESSION_COOKIE_NAME].value)
    assert ok and frames and "cc1:" in str(frames)
    # An ordinary conversation still opens to the minted cookie: it is not a person,
    # but nothing there is sealed.
    ok, _ = _connect(f"/ws/canopy-sessions/{w['cold'].id}/", cookie)
    assert ok


def test_the_socket_scope_says_which_session_signed_it_in():
    assert sensitivity.scope_is_chat_surface({"auth_method": "session"})
    assert sensitivity.scope_is_chat_surface({"auth_method": "dlg", "via_widget": True})
    assert not sensitivity.scope_is_chat_surface({"auth_method": "minted"})
    assert not sensitivity.scope_is_chat_surface({"auth_method": "minted", "via_widget": True})
    assert not sensitivity.scope_is_chat_surface({"auth_method": "pat"})
    assert not sensitivity.scope_is_chat_surface({})


# --- 2. a Slack reply never makes anyone one of its people ------------------------


@pytest.fixture()
def private_thread(slack, linked, hal, alice, installation, ws):
    """Alice's Slack thread with hal, since become sensitive; Bob is a linked member."""
    from apps.slack.models import SlackUserLink
    from tests.fixtures.slack import BOB, mention

    vault_key(alice)
    mention("hal how many visits did Amina have?")
    session = Session.objects.get()
    services.add_runner_requirements(session, {"sensitive_data"})
    bob = User.objects.create_user("bob", "bob@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=bob, workspace=ws, role=WorkspaceMembership.EDITOR)
    vault_key(bob)
    SlackUserLink.objects.create(installation=installation, slack_user_id=BOB, user=bob)
    session.refresh_from_db()
    return session, bob


@pytest.mark.usefixtures("slack_configured")
def test_a_slack_reply_into_a_private_conversation_adds_nobody_and_sends_nothing(private_thread, slack):
    from tests.fixtures.slack import BOB, thread_reply

    session, bob = private_thread
    turns = Turn.objects.count()
    assert thread_reply("what about her HIV status?", user=BOB).status_code == 200
    assert not SessionParticipant.objects.filter(session=session, user=bob).exists()
    assert not sensitivity.reads_content(session, chat_surface=True, user=bob)
    assert not SessionKeyWrap.objects.filter(session=session, user=bob).exists()
    assert Turn.objects.count() == turns
    told = " ".join(p.get("text", "") for p in slack.said("chat.postEphemeral") + slack.said("chat.postMessage"))
    assert "private" in told and f"/chat/{session.pk}" in told


@pytest.mark.usefixtures("slack_configured")
def test_even_its_owner_cannot_send_into_it_from_slack_or_branch_it(private_thread, slack):
    from tests.fixtures.slack import mention, thread_reply

    session, _bob = private_thread
    turns, sessions = Turn.objects.count(), Session.objects.count()
    thread_reply("and last month?")
    mention("branch try it another way", ts="1700000080.000100", thread_ts="1700000000.000100")
    assert Turn.objects.count() == turns and Session.objects.count() == sessions


def test_no_door_adds_a_participant_to_a_sensitive_conversation_implicitly(w):
    from apps.canopy_sessions.participants import ensure_participant

    bob = _member(w, "bob")
    assert ensure_participant(w["hot"], bob) is None
    assert not SessionParticipant.objects.filter(session=w["hot"], user=bob).exists()
    assert ensure_participant(w["cold"], bob) is not None


# --- 3. becoming sensitive seals what it already said -----------------------------


def test_rows_from_before_it_became_sensitive_are_sealed_when_it_does(w):
    cold = w["cold"]
    # A runner-discovered one: its agent's admins can open it, and are not in it.
    cold.origin = Session.ORIGIN_RUNNER
    cold.save(update_fields=["origin"])
    _converse(cold)
    turn = _turn(cold, prompt=ASKED, status=Turn.DONE, result_note=f"read {ROW}")
    TurnEvent.objects.create(turn=turn, seq=1, kind="assistant", payload={"text": REPLY})
    TurnEvent.objects.create(turn=turn, seq=2, kind="tool_end", payload={"tool_use_id": "tu1", "text": ROW})
    live = _turn(cold, prompt="and the next one?", status=Turn.QUEUED)
    RunnerBinding.objects.create(session=cold, runner=w["runner"], session_key="k", tail=[{"text": REPLY}],
                                 summary=f"Amina: {REPLY}",
                                 pending_question={"question": "Show Amina's record?", "options": []})
    # What makes a conversation sensitive from every door: its requirements grow.
    services.add_runner_requirements(cold, {"sensitive_data"})

    stored = str(list(Message.objects.filter(session=cold).values("plaintext", "content")))
    assert "Amina" not in stored and REPLY not in stored and ROW not in stored
    rows = {m.role: m for m in Message.objects.filter(session=cold)}
    assert rows["tool_result"].sealed is None  # a tool's result is never kept
    # The words are sealed to the conversation's key, which its people's browsers open.
    said = open_sealed(rows["assistant"].sealed, cold, w["key"], w["user"])
    assert said["plaintext"] == REPLY
    turn.refresh_from_db()
    live.refresh_from_db()
    assert turn.prompt == "" and ROW not in turn.result_note
    assert live.prompt == "and the next one?"  # still to run: wiped when it ends
    assert "Amina" not in str(list(TurnEvent.objects.filter(turn=turn).values_list("payload", flat=True)))
    binding = RunnerBinding.objects.get(session=cold)
    assert binding.tail == [] and binding.summary == "" and "Amina" not in str(binding.pending_question)
    # And an agent admin who is not in it reads none of it back.
    body = _browser(_admin(w)).get(f"/api/canopy-sessions/{cold.pk}?full=true").json()
    assert body["you_are_in_it"] is False and "Amina" not in str(body["messages"])


# --- 4. a sensitive turn no runner starts does not keep its prompt -------------------


def test_an_unrun_sensitive_turn_is_closed_at_its_deadline_by_the_tick(w):
    from apps.common import ticks

    old = timezone.now() - dt.timedelta(hours=2)
    stale = _turn(w["hot"], prompt=ASKED, status=Turn.QUEUED)
    claimed = _turn(w["hot"], prompt=ASKED, status=Turn.CLAIMED, claimed_by=w["runner"])
    fresh = _turn(w["hot"], prompt="just now", status=Turn.QUEUED)
    ordinary = _turn(w["cold"], prompt="old but ordinary", status=Turn.QUEUED)
    Turn.objects.filter(pk__in=[stale.pk, claimed.pk, ordinary.pk]).update(created_at=old)
    ticks.jobs()["harness.sensitive_turn_deadline"].fn()
    for t in (stale, claimed, fresh, ordinary):
        t.refresh_from_db()
    assert (stale.status, stale.prompt) == (Turn.MISSED, "")
    assert (claimed.status, claimed.prompt) == (Turn.MISSED, "")
    assert (fresh.status, fresh.prompt) == (Turn.QUEUED, "just now")
    assert (ordinary.status, ordinary.prompt) == (Turn.QUEUED, "old but ordinary")


def test_the_sensitive_sweep_closes_them_too(w):
    from apps.retention import services as retention

    stale = _turn(w["hot"], prompt=ASKED, status=Turn.QUEUED)
    Turn.objects.filter(pk=stale.pk).update(created_at=timezone.now() - dt.timedelta(hours=2))
    retention.sweep(apply=True, sensitive_only=True, trigger="heartbeat")
    stale.refresh_from_db()
    assert (stale.status, stale.prompt) == (Turn.MISSED, "")


# --- 5. its page state is behind the gate ----------------------------------------


def test_page_state_reads_back_only_to_its_people_in_a_browser(w):
    page_state.set_page_state(w["hot"], PHI)
    url = f"/api/canopy-sessions/{w['hot'].pk}/page-state"
    assert w["c"].get(url).status_code == 200
    assert minted(w["user"]).get(url).status_code == 403
    unowned = _unowned_thread(w)
    page_state.set_page_state(unowned, PHI)
    r = _browser(_admin(w)).get(f"/api/canopy-sessions/{unowned.pk}/page-state")
    assert r.status_code == 403 and b"Amina" not in r.content


def test_current_page_over_mcp_leaves_out_a_sensitive_conversations_page(w):
    from apps.mcp.server import mcp

    page_state.set_page_state(w["hot"], PHI)
    page_state.set_page_state(w["cold"], {"filters": {"opp": "kmc"}})
    with mcp_as(w["user"]):
        out = async_to_sync(mcp.call_tool)("current_page", {})
    text = str(out)
    assert "kmc" in text and "Amina" not in text


def test_a_sensitive_conversations_page_is_forgotten_when_its_turn_ends(w):
    from apps.harness.closing import close_turn

    page_state.set_page_state(w["hot"], PHI)
    turn = _turn(w["hot"], status=Turn.RUNNING, claimed_by=w["runner"])
    close_turn(turn, Turn.DONE, from_states=[Turn.RUNNING])
    w["hot"].refresh_from_db()
    assert w["hot"].page_state == {}


def test_a_sensitive_page_no_turn_ends_for_is_forgotten_after_its_ttl(w):
    page_state.set_page_state(w["hot"], PHI)
    page_state.set_page_state(w["cold"], {"filters": {"opp": "kmc"}})
    soon = timezone.now() + page_state.sensitive_page_ttl() + dt.timedelta(seconds=5)
    # Within its TTL, or while a turn of it is live, it stays.
    assert page_state.forget_idle_sensitive_pages(now=timezone.now()) == 0
    live = _turn(w["hot"], status=Turn.RUNNING, claimed_by=w["runner"])
    assert page_state.forget_idle_sensitive_pages(now=soon) == 0
    Turn.objects.filter(pk=live.pk).update(status=Turn.DONE)
    assert page_state.forget_idle_sensitive_pages(now=soon) == 1
    w["hot"].refresh_from_db()
    w["cold"].refresh_from_db()
    assert w["hot"].page_state == {} and w["cold"].page_state["filters"] == {"opp": "kmc"}


# --- 5b. its page's actions are behind the same gate -----------------------------

ACTION = {"name": "flagVisit", "description": "Flag Amina Okafor's visit",
          "parameters": {"type": "object", "properties": {"id": {"type": "string"}}}}


def _pages(w):
    from apps.canopy_sessions import page_actions

    for s in (w["hot"], w["cold"]):
        page_actions.set_declared_actions(s, [ACTION if s is w["hot"] else {"name": "closeTasks"}])


def test_page_actions_rest_routes_serve_only_its_people_in_a_browser(w):
    _pages(w)
    base = f"/api/canopy-sessions/{w['hot'].pk}/page-actions"
    assert w["c"].get(base).status_code == 200
    token = minted(w["user"])
    for r in (token.get(base),
              token.put(base, {"actions": [ACTION]}, content_type="application/json"),
              token.post(f"{base}/invoke", {"name": "flagVisit", "args": {"id": "c-1"}},
                         content_type="application/json"),
              token.put(f"/api/canopy-sessions/{w['hot'].pk}/page-state", {"state": PHI},
                        content_type="application/json"),
              token.put(f"/api/canopy-sessions/{w['hot'].pk}/run-input",
                        {"state": PHI, "tools": [ACTION]}, content_type="application/json")):
        assert r.status_code == 403 and b"Amina" not in r.content, r.content
    # An admin of its agent who is not in it reads nothing of its page either.
    unowned = _unowned_thread(w)
    from apps.canopy_sessions import page_actions

    page_actions.set_declared_actions(unowned, [ACTION])
    r = _browser(_admin(w)).get(f"/api/canopy-sessions/{unowned.pk}/page-actions")
    assert r.status_code == 403 and b"Amina" not in r.content
    # An ordinary conversation's page is unchanged.
    assert minted(w["user"]).get(f"/api/canopy-sessions/{w['cold'].pk}/page-actions").json()[0][
        "name"] == "closeTasks"


def test_a_page_actions_answer_cannot_be_resolved_through_a_token(w):
    from apps.canopy_sessions.models import PageAction

    action = PageAction.objects.create(session=w["hot"], name="flagVisit")
    r = minted(w["user"]).post(f"/api/canopy-sessions/{w['hot'].pk}/page-actions/{action.pk}/result",
                               {"result": {"forged": True}}, content_type="application/json")
    assert r.status_code == 403
    action.refresh_from_db()
    assert action.status == PageAction.PENDING and action.result is None


def test_page_action_tools_over_mcp_leave_out_a_sensitive_conversation(w):
    from fastmcp.exceptions import ToolError

    from apps.mcp.page_tools import PageActionProvider
    from apps.mcp.server import mcp

    _pages(w)
    with mcp_as(w["user"]):
        tools = async_to_sync(PageActionProvider()._list_tools)()
        listed = async_to_sync(mcp.call_tool)("list_page_actions", {"session_id": str(w["cold"].pk)})
        with pytest.raises(ToolError) as listing:
            async_to_sync(mcp.call_tool)("list_page_actions", {"session_id": str(w["hot"].pk)})
        with pytest.raises(ToolError) as invoking:
            async_to_sync(mcp.call_tool)("invoke_page_action", {
                "session_id": str(w["hot"].pk), "payload": {"name": "flagVisit", "args": {}}})
    assert [t.name for t in tools] == ["page_closeTasks"]
    assert "closeTasks" in str(listed)
    for err in (listing, invoking):
        assert "Amina" not in str(err.value) and "flagVisit" not in str(err.value)


def test_the_turn_running_in_it_still_acts_on_its_page_through_its_chat_key(w, monkeypatch):
    from unittest import mock

    from fastmcp.exceptions import ToolError

    from apps.canopy_sessions import chat_keys, page_actions
    from apps.canopy_sessions.models import PageAction
    from apps.mcp.page_tools import PageActionProvider, to_mcp_tool

    _pages(w)
    monkeypatch.setattr(page_actions, "DEFAULT_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(page_actions, "POLL_INTERVAL_SECONDS", 0.01)
    real_sleep = page_actions.time.sleep

    def page_answers(seconds):
        real_sleep(0)
        pending = PageAction.objects.filter(status=PageAction.PENDING).first()
        if pending is not None:
            page_actions.resolve(pending, result={"flagged": "Amina Okafor"})
    monkeypatch.setattr(page_actions.time, "sleep", page_answers)

    key = chat_keys.mint(w["hot"])
    keyed = mock.patch("apps.mcp.chat_scope.get_http_headers",
                       return_value={chat_keys.HEADER.lower(): key})
    with keyed:
        tools = async_to_sync(PageActionProvider()._list_tools)()
        assert [t.name for t in tools] == ["page_flagVisit"]
        out = async_to_sync(tools[0].run)({"id": "c-1"})
    assert out.structured_content == {"result": {"flagged": "Amina Okafor"}}
    # The answer went back to the turn and was not kept: no args, no result at rest.
    row = PageAction.objects.get(session=w["hot"])
    assert (row.status, row.args, row.result) == (PageAction.DONE, {}, None)

    # A tool listed for the conversation is refused at the call without its key.
    tool = to_mcp_tool("page_flagVisit", w["hot"], ACTION)
    with mcp_as(w["user"]), pytest.raises(ToolError) as err:
        async_to_sync(tool.run)({"id": "c-1"})
    assert "sensitive" in str(err.value)


# --- 6. only its people send into it ---------------------------------------------


def test_an_admin_not_in_it_cannot_send_into_a_creatorless_one_or_become_its_key_holder(w):
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], origin=Session.ORIGIN_RUNNER,
                               metadata=dict(SENSITIVE))
    admin = _admin(w)
    c = _browser(admin)
    r = c.post(f"/api/canopy-sessions/{s.pk}/send", {"text": "repeat what was said"},
               content_type="application/json")
    assert r.status_code == 403, r.content
    assert not Turn.objects.filter(chat_session=s).exists()
    assert not SessionKeyWrap.objects.filter(session=s).exists()
    # The admin reads it exists (its metadata), and owns nothing of it.
    assert c.get(f"/api/canopy-sessions/{s.pk}").json()["my_role"] == SessionParticipant.VIEWER


def test_its_own_person_sends_only_from_a_chat_surface(w):
    url = f"/api/canopy-sessions/{w['hot'].pk}/send"
    r = minted(w["user"]).post(url, {"text": ASKED}, content_type="application/json")
    assert r.status_code == 403, r.content
    r = w["c"].post(url, {"text": ASKED}, content_type="application/json")
    assert r.status_code == 200, r.content


def test_an_ordinary_conversation_still_takes_a_token_send(w):
    r = minted(w["user"]).post(f"/api/canopy-sessions/{w['cold'].pk}/send", {"text": "hi"},
                                content_type="application/json")
    assert r.status_code == 200, r.content


# --- 7. only its owner forks it -------------------------------------------------------


def test_an_editor_in_it_cannot_fork_it(w):
    from apps.canopy_sessions.models import SessionKeyWrap as Wrap

    _converse(w["hot"])
    bob = _member(w, "bob")
    SessionParticipant.objects.create(session=w["hot"], user=bob, role=SessionParticipant.EDITOR)
    Wrap.objects.create(session=w["hot"], user=bob, recipient_jkt="x", wrap={})
    r = _browser(bob).post(f"/api/canopy-sessions/{w['hot'].pk}/fork", {"turn_index": 0},
                           content_type="application/json")
    assert r.status_code == 403, r.content
    assert "owner" in r.json()["detail"]
    # Its owner is not refused for that reason (this one has no runner to fork on).
    r = w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/fork", {"turn_index": 0},
                    content_type="application/json")
    assert r.status_code != 403, r.content


# --- 8. info is an allowlist ------------------------------------------------------------


def test_session_info_shows_no_bookkeeping_metadata_and_no_box_session(w):
    hot = w["hot"]
    hot.metadata = {**hot.metadata, "turn_end_epochs": {"cli-secret-1": 0}, "opp_slug": "kmc",
                    "mcp_apps": {"receipts": ["x"]}}
    hot.save(update_fields=["metadata"])
    RunnerBinding.objects.create(session=hot, runner=w["runner"], session_key="k",
                                 transcript_id="cli-secret-1", summary="Amina's visits")
    body = w["c"].get(f"/api/canopy-sessions/{hot.pk}/info").json()
    assert body["metadata"] == {"runner_requirements": ["sensitive_data"], "opp_slug": "kmc"}
    assert body["binding"]["transcript_id"] == "" and body["binding"]["summary"] == ""
    assert "cli-secret-1" not in str(body) and "Amina" not in str(body)


# --- 9. the unbound stream door takes only the turn's own transcript --------------------


def _ship(client, runner, session, transcript_id, text=REPLY):
    return client.post(f"/api/harness/runners/{runner.id}/session-stream", data={
        "session_id": str(session.id), "transcript_id": transcript_id,
        "events": [{"kind": "assistant", "seq": 0, "index": 0, "payload": {"text": text}}]},
        content_type="application/json")


def test_the_unbound_stream_door_takes_only_the_running_turns_own_transcript(w):
    turn = _turn(w["hot"], capability="data", claimed_by=w["runner"], status=Turn.RUNNING)
    assert _ship(w["c"], w["runner"], w["hot"], "").status_code == 404
    assert _ship(w["c"], w["runner"], w["hot"], "cli-1").status_code == 200
    turn.refresh_from_db()
    assert turn.session_key == "cli-1"  # the first transcript it shipped is the turn's
    assert _ship(w["c"], w["runner"], w["hot"], "cli-1", "more").status_code == 200
    assert _ship(w["c"], w["runner"], w["hot"], "cli-other", "planted").status_code == 404


def test_the_unbound_stream_door_asks_whether_the_box_may_still_hold_the_agent(w):
    from apps.harness.models import Runner

    stranger = User.objects.create_user("eve", "eve@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=stranger, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    box = Runner.objects.create(name="eve-box", workspace=w["ws"], location=Runner.LOCAL,
                                status=Runner.ONLINE, owner=stranger)
    _turn(w["hot"], capability="data", claimed_by=box, status=Turn.RUNNING)
    assert _ship(_browser(stranger), box, w["hot"], "cli-1").status_code == 404


# --- 10. a list reads who is in which once --------------------------------------------


def test_the_list_asks_who_is_in_each_sensitive_conversation_once(w):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert w["c"].get("/api/canopy-sessions/").status_code == 200
        return sum("sessionparticipant" in q["sql"].lower() for q in ctx.captured_queries)

    other = _member(w, "bob")
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=other,
                               metadata=dict(SENSITIVE))
    SessionParticipant.objects.create(session=s, user=w["user"], role=SessionParticipant.VIEWER)
    one = count()
    for _ in range(5):
        s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=other,
                                   metadata=dict(SENSITIVE))
        SessionParticipant.objects.create(session=s, user=w["user"], role=SessionParticipant.VIEWER)
    assert count() == one


# --- 11. its done push says only that a reply is ready -----------------------------------


def test_a_sensitive_turns_done_push_reads_reply_ready(w):
    from apps.push.services import _finish_body

    turn = _turn(w["hot"], status=Turn.DONE, result_note=f"Amina: {REPLY}")
    Message.objects.create(session=w["hot"], turn_index=5, role=Message.ASSISTANT, plaintext=REPLY)
    assert _finish_body(w["hot"], turn) == "Reply ready"
    turn.status = Turn.FAILED
    assert _finish_body(w["hot"], turn) == "The turn failed"


# --- 12. a declined requester waits before asking again ---------------------------------


def test_asking_to_join_again_after_a_decline_waits_an_hour(w):
    # Runner-origin, so an admin of its agent sees it exists; jj started it, so jj decides.
    Session.objects.filter(pk=w["hot"].pk).update(origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=w["hot"], runner=w["runner"], session_key="k")
    bob = _admin(w, "bob")
    c = _browser(bob)
    url = f"/api/canopy-sessions/{w['hot'].pk}/join-requests"
    first = c.post(url)
    assert first.status_code == 200, first.content
    r = w["c"].post(f"/api/canopy-sessions/join-requests/{first.json()['id']}/decline")
    assert r.status_code == 200, r.content
    again = c.post(url)
    assert again.status_code == 429, again.content
    JoinRequest.objects.filter(session=w["hot"]).update(created_at=timezone.now() - dt.timedelta(hours=2))
    assert c.post(url).status_code == 200


# --- 13. the person's keyset goes only to the panel's browser ---------------------------


def test_the_keyset_reaches_the_panels_browser_never_the_hosts_backend(w):
    from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken

    app = AppCredential.create_credential(name="connect-labs", created_by=w["user"], workspace=w["ws"])
    AppCredentialAgent.objects.create(app=app, agent=w["agent"])
    raw, _ = DelegatedToken.issue(app=app, user=w["user"], ttl_seconds=600,
                                  runner_requirements=["sensitive_data"])
    url = f"/api/canopy-sessions/{w['hot'].pk}"
    backend = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get(url)
    assert backend.status_code == 200, backend.content
    assert backend.json()["sealed_key"]["keyset"] is None
    panel = Client(HTTP_AUTHORIZATION=f"Bearer {raw}", HTTP_SEC_FETCH_SITE="same-origin").get(url)
    assert panel.json()["sealed_key"]["keyset"]["user_id"] == str(w["user"].pk)
    # canopy's own chat page, signed in, is the person's browser.
    assert w["c"].get(url).json()["sealed_key"]["keyset"] is not None
