"""Agent threads — bounded, moderated agent→agent conversations (apps/threads).

The thread row is the frame and its limits; each message is a harness turn tagged
`origin_ref.kind == "thread_message"`, and the limits are enforced where that turn
is created (the GUARD in `harness.services.enqueue_turn`). These tests pin the
endpoints, every guard refusal, and the messages derived back out of the turns."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.threads.models import AgentThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@example.org", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=w, role=WorkspaceMembership.OWNER)
    return w


@pytest.fixture()
def agents(ws):
    return {s: Agent.objects.create(slug=s, name=s.title(), workspace=ws)
            for s in ("ada", "eva", "echo", "hal")}


def _client(user):
    c = Client()
    c.force_login(user)
    return c


BODY = {
    "kind": "agreement",
    "purpose": "Settle Echo's changes to Eva's idea",
    "participants": [{"agent": "eva", "role": "author"}, {"agent": "echo", "role": "asker"}],
    "moderator": "ada",
    "parent": {"huddle": "h1", "title": "Funder brief", "lead": "eva"},
    "context": "The idea, verbatim. The change asked for, verbatim.",
}


def _open(c, **over):
    return c.post("/api/threads/", {**BODY, **over}, content_type="application/json")


def _say(c, tid, n, speaker, *, agent=None, key=None):
    return c.post("/api/harness/turns/", {
        "agent_slug": agent or speaker, "origin": "api", "prompt": f"message {n}",
        "idempotency_key": key or f"thread-{tid}-n{n}",
        "origin_ref": {"kind": "thread_message", "thread": tid, "n": n, "speaker": speaker},
    }, content_type="application/json")


# ── open / list / get / close ────────────────────────────────────────────────


def test_open_thread(owner, agents):
    r = _open(_client(owner))
    assert r.status_code == 201, r.content
    d = r.json()
    assert d["id"].startswith("thr-") and len(d["id"]) == 16
    assert d["status"] == "open" and d["max_messages"] == 4 and d["messages_used"] == 0
    assert d["moderator"] == "ada" and d["parent"]["huddle"] == "h1"
    assert [p["agent"] for p in d["participants"]] == ["eva", "echo"]
    t = AgentThread.objects.get(id=d["id"])
    assert t.workspace_id == "connect" and t.created_by == owner
    assert dt.timedelta(minutes=89) < t.deadline_at - t.created_at <= dt.timedelta(minutes=90)


def test_reopen_returns_the_open_thread(owner, agents):
    c = _client(owner)
    first = _open(c).json()
    # Same parent, same agents (any order): resumable, not a duplicate.
    again = _open(c, participants=list(reversed(BODY["participants"])))
    assert again.status_code == 200 and again.json()["id"] == first["id"]
    other = _open(c, parent={**BODY["parent"], "title": "Another idea"})
    assert other.status_code == 201 and other.json()["id"] != first["id"]
    # Once closed, the same request opens a fresh thread.
    c.post(f"/api/threads/{first['id']}/close", {"status": "cancelled"}, content_type="application/json")
    assert _open(c).status_code == 201


@pytest.mark.parametrize("over,why", [
    ({"participants": [{"agent": "eva"}]}, "2 to 6"),
    ({"participants": [{"agent": "eva"}, {"agent": "eva"}]}, "distinct"),
    ({"max_messages": 13}, ""),
    ({"max_messages": 0}, ""),
])
def test_open_validates(owner, agents, over, why):
    r = _open(_client(owner), **over)
    assert r.status_code == 422
    assert why in r.content.decode()


def test_open_refuses_unknown_agents_and_viewers(owner, agents, ws):
    assert _open(_client(owner), participants=[{"agent": "eva"}, {"agent": "nobody"}]).status_code == 404
    viewer = User.objects.create_user("v", "v@example.org", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, role=WorkspaceMembership.VIEWER)
    assert _open(_client(viewer)).status_code == 403
    stranger = User.objects.create_user("s", "s@example.org", "pw")
    assert _open(_client(stranger)).status_code == 404


def test_open_refused_from_inside_a_thread_message(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    msg = Turn.objects.get(id=_say(c, tid, 1, "eva").json()["id"])
    r = c.post("/api/threads/", {**BODY, "parent": {"huddle": "h2"}}, content_type="application/json",
               HTTP_X_CANOPY_PARENT_TURN=str(msg.id))
    assert r.status_code == 422 and "inside a thread message" in r.content.decode()
    # From an ordinary turn it is fine.
    plain = Turn.objects.create(agent=agents["ada"], idempotency_key="plain")
    r = c.post("/api/threads/", {**BODY, "parent": {"huddle": "h2"}}, content_type="application/json",
               HTTP_X_CANOPY_PARENT_TURN=str(plain.id))
    assert r.status_code == 201


def test_list_filters(owner, agents):
    c = _client(owner)
    a = _open(c).json()["id"]
    b = _open(c, parent={"huddle": "h2", "title": "x", "lead": "hal"},
              participants=[{"agent": "hal"}, {"agent": "eva"}]).json()["id"]
    assert [t["id"] for t in c.get("/api/threads/").json()] == [b, a]
    assert [t["id"] for t in c.get("/api/threads/?parent_key=huddle&parent_value=h1").json()] == [a]
    assert [t["id"] for t in c.get("/api/threads/?agent=hal").json()] == [b]
    assert [t["id"] for t in c.get("/api/threads/?agent=echo").json()] == [a]
    assert c.get("/api/threads/?status=settled").json() == []
    assert c.get("/api/threads/?parent_key=huddle__x&parent_value=1").status_code == 422
    assert [t["id"] for t in c.get("/api/w/connect/threads/").json()] == [b, a]
    _say(c, a, 1, "eva")
    row = next(t for t in c.get("/api/threads/").json() if t["id"] == a)
    assert row["messages_used"] == 1 and row["messages"] == []


def test_strangers_see_nothing(owner, agents):
    tid = _open(_client(owner)).json()["id"]
    stranger = User.objects.create_user("s", "s@example.org", "pw")
    assert _client(stranger).get("/api/threads/").json() == []
    assert _client(stranger).get(f"/api/threads/{tid}").status_code == 404


def test_close(owner, agents, ws):
    c = _client(owner)
    tid = _open(c).json()["id"]
    outcome = {"result": "agreed", "proposal": {"title": "Funder brief v2"}, "why": "both agree"}
    r = c.post(f"/api/threads/{tid}/close", {"status": "settled", "outcome": outcome},
               content_type="application/json")
    assert r.status_code == 200, r.content
    d = r.json()
    assert d["status"] == "settled" and d["outcome"] == outcome and d["closed_at"]
    again = c.post(f"/api/threads/{tid}/close", {"status": "cancelled"}, content_type="application/json")
    assert again.status_code == 409
    assert c.post(f"/api/threads/{tid}/close", {"status": "open"},
                  content_type="application/json").status_code == 422
    viewer = User.objects.create_user("v", "v@example.org", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, role=WorkspaceMembership.VIEWER)
    tid2 = _open(c, parent={"huddle": "h9"}).json()["id"]
    assert _client(viewer).post(f"/api/threads/{tid2}/close", {"status": "cancelled"},
                                content_type="application/json").status_code == 403


# ── the GUARD ────────────────────────────────────────────────────────────────


def test_guard_allows_messages_in_order(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    assert _say(c, tid, 1, "eva").status_code == 201
    assert _say(c, tid, 2, "echo").status_code == 201
    assert c.get(f"/api/threads/{tid}").json()["messages_used"] == 2


def test_guard_idempotent_retry_returns_existing_turn(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    first = _say(c, tid, 1, "eva")
    # n=1 is no longer next, but the same idempotency key is the same message.
    retry = _say(c, tid, 1, "eva")
    assert retry.status_code == 200 and retry.json()["id"] == first.json()["id"]
    # Even once the thread is closed.
    c.post(f"/api/threads/{tid}/close", {"status": "cancelled"}, content_type="application/json")
    assert _say(c, tid, 1, "eva").status_code == 200


def test_guard_refuses_unknown_thread(owner, agents):
    r = _say(_client(owner), "thr-000000000000", 1, "eva")
    assert r.status_code == 409 and "does not exist" in r.content.decode()


def test_guard_refuses_closed_thread(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    c.post(f"/api/threads/{tid}/close", {"status": "settled"}, content_type="application/json")
    r = _say(c, tid, 1, "eva")
    assert r.status_code == 409 and f"thread {tid} is settled" in r.content.decode()
    assert not Turn.objects.filter(origin_ref__thread=tid).exists()


def test_guard_refuses_past_deadline_and_leaves_status(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    AgentThread.objects.filter(id=tid).update(deadline_at=timezone.now() - dt.timedelta(seconds=1))
    r = _say(c, tid, 1, "eva")
    assert r.status_code == 409 and "passed its deadline" in r.content.decode()
    assert AgentThread.objects.get(id=tid).status == "open"


def test_guard_refuses_over_budget(owner, agents):
    c = _client(owner)
    tid = _open(c, max_messages=2).json()["id"]
    assert _say(c, tid, 1, "eva").status_code == 201
    assert _say(c, tid, 2, "echo").status_code == 201
    r = _say(c, tid, 3, "eva")
    assert r.status_code == 409 and "used its 2 messages" in r.content.decode()


def test_guard_budget_ignores_closeout_rows(owner, agents):
    c = _client(owner)
    tid = _open(c, max_messages=2).json()["id"]
    _say(c, tid, 1, "eva")
    # The speaker's report-only close-out may carry the message's origin_ref; it is
    # the reply, not another message.
    c.post("/api/agents/eva/turns/", {
        "cli_session_id": f"thread:{tid}:1", "title": f"thread {tid} message 1",
        "origin_ref": {"kind": "thread_message", "thread": tid, "n": 1, "speaker": "eva"}},
        content_type="application/json")
    assert _say(c, tid, 2, "echo").status_code == 201


def test_guard_refuses_non_participant(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    r = _say(c, tid, 1, "hal")
    assert r.status_code == 422 and "not a participant" in r.content.decode()


def test_guard_refuses_speaker_other_than_target(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    r = _say(c, tid, 1, "eva", agent="echo")
    assert r.status_code == 422 and "spoken by its speaker" in r.content.decode()


@pytest.mark.parametrize("n", [0, 2, 5, "x"])
def test_guard_refuses_out_of_order(owner, agents, n):
    c = _client(owner)
    tid = _open(c).json()["id"]
    r = _say(c, tid, n, "eva", key=f"k-{n}")
    assert r.status_code == 409 and "is on message 1" in r.content.decode()


def test_guard_covers_the_service_path(owner, agents):
    """Every producer goes through services.enqueue_turn, not only the REST route."""
    from apps.harness import services as hsvc
    from apps.threads.guard import ThreadRefused

    tid = _open(_client(owner)).json()["id"]
    AgentThread.objects.filter(id=tid).update(status="cancelled")
    with pytest.raises(ThreadRefused) as e:
        hsvc.enqueue_turn(agent=agents["eva"], origin="api", idempotency_key="svc",
                          origin_ref={"kind": "thread_message", "thread": tid, "n": 1, "speaker": "eva"})
    assert e.value.status_code == 409


def test_other_origin_refs_are_untouched(owner, agents):
    r = _client(owner).post("/api/harness/turns/", {
        "agent_slug": "eva", "origin": "api", "idempotency_key": "plain",
        "origin_ref": {"kind": "huddle_round", "huddle": "h1"}}, content_type="application/json")
    assert r.status_code == 201


# ── the reply block and derived messages ─────────────────────────────────────


def _block(tid, n, frm, says="Fine by me.", position="agree", **extra):
    return "```thread\n" + json.dumps({"thread": tid, "n": n, "from": frm, "says": says,
                                       "position": position, **extra}) + "\n```"


def test_extract_block_rules():
    from apps.threads.services import extract_block
    good = _block("thr-a", 2, "eva")
    assert extract_block("prose\n" + good, "thr-a", 2)[0]["from"] == "eva"
    b, why = extract_block(good, "thr-a", 1)
    assert b is None and "message 2" in why
    assert extract_block("no block here", "thr-a", 1) == (None, "")
    loose = '```json\n{"thread": "thr-a", "n": 1, "says": "loose"}\n```'
    assert extract_block(loose, "thr-a", 1)[0]["says"] == "loose"
    bare = '{"thread": "thr-a", "n": 1, "says": "bare"}'
    assert extract_block(bare, "thr-a", 1)[0]["says"] == "bare"
    # Unlabelled JSON without a "thread" key is prose, not a reply.
    assert extract_block('```json\n{"n": 1}\n```', "thr-a", 1) == (None, "")
    # A labelled block wins over a loose one, and the LAST labelled one wins.
    both = _block("thr-a", 1, "eva", says="first") + "\n" + _block("thr-a", 1, "eva", says="last") + "\n" + loose
    assert extract_block(both, "thr-a", 1)[0]["says"] == "last"
    assert "not valid JSON" in extract_block("```thread\n{nope\n```", "thr-a", 1)[1]


def test_messages_derived_from_turns(owner, agents):
    c = _client(owner)
    tid = _open(c).json()["id"]
    m1 = Turn.objects.get(id=_say(c, tid, 1, "eva").json()["id"])
    m2 = Turn.objects.get(id=_say(c, tid, 2, "echo").json()["id"])
    # 1: a cloud runner's report lands on the message turn itself.
    Turn.objects.filter(pk=m1.pk).update(status=Turn.DONE, report_summary=_block(
        tid, 1, "eva", says="Here is the revised idea.", position="counter", proposal={"title": "v2"}))
    # 2: a laptop session files a report-only close-out row.
    c.post("/api/agents/echo/turns/", {
        "cli_session_id": f"thread:{tid}:2", "title": f"thread {tid} message 2",
        "summary": _block(tid, 2, "echo", says="Agreed.")}, content_type="application/json")
    d = c.get(f"/api/threads/{tid}").json()
    assert d["messages_used"] == 2
    assert [(m["n"], m["speaker"]) for m in d["messages"]] == [(1, "eva"), (2, "echo")]
    one, two = d["messages"]
    assert one["block"]["position"] == "counter" and one["block"]["proposal"] == {"title": "v2"}
    assert one["reply_source"] == "closeout" and one["prompt"] == "message 1"
    assert two["block"]["says"] == "Agreed." and two["reply_source"] == "closeout"
    assert two["turn_id"] == str(m2.id) and two["status"] == "queued"


def test_message_reply_from_transcript_and_errors(owner, agents):
    from apps.harness import ledger
    c = _client(owner)
    tid = _open(c).json()["id"]
    m1 = Turn.objects.get(id=_say(c, tid, 1, "eva").json()["id"])
    line = json.dumps({"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "text", "text": "Here you go\n" + _block(tid, 1, "eva")}]}})
    ledger.append_transcript(m1, [line])
    msg = c.get(f"/api/threads/{tid}").json()["messages"][0]
    assert msg["reply_source"] == "transcript" and msg["block"]["position"] == "agree"
    m2 = Turn.objects.get(id=_say(c, tid, 2, "echo").json()["id"])
    Turn.objects.filter(pk=m2.pk).update(report_summary=_block("thr-other", 2, "echo"))
    msg2 = c.get(f"/api/threads/{tid}").json()["messages"][1]
    assert msg2["block"] is None and "thr-other" in msg2["reply_error"]


def test_message_content_is_gated(owner, agents, ws):
    c = _client(owner)
    tid = _open(c).json()["id"]
    m1 = Turn.objects.get(id=_say(c, tid, 1, "eva").json()["id"])
    Turn.objects.filter(pk=m1.pk).update(report_summary=_block(tid, 1, "eva"))
    viewer = User.objects.create_user("v", "v@example.org", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, role=WorkspaceMembership.VIEWER)
    from apps.harness import turn_access
    if turn_access.can_read_turn_content(viewer, Turn.objects.get(pk=m1.pk), {}):
        pytest.skip("viewers may read this turn's content under the current access rules")
    msg = _client(viewer).get(f"/api/threads/{tid}").json()["messages"][0]
    assert msg["content_hidden"] is True and msg["prompt"] == "" and msg["block"] is None
