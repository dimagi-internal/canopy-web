"""T73 — walking EVERY session (`GET /canopy-sessions/search`, cursor-paged, with
q/repo/since/until), and T77 — reading only what people typed into one
(`GET /canopy-sessions/{id}/human-inputs`).

The bare list (`GET /canopy-sessions/`) keeps its shape: a plain array, waiting
and running first, capped at 500. The cursor walk is a separate route so the
list's existing callers (the web UI, the canopy CLI, MCP) are untouched.
"""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

T0 = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)


def _ctx(username="jj"):
    user = User.objects.create_user(username, f"{username}@dimagi.com", "pw")
    ws = Workspace.objects.create(slug=f"w-{username}", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    return user, ws, c


def _runner(ws, user):
    return Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                 status=Runner.ONLINE, last_heartbeat_at=timezone.now(), owner=user)


def _web(ws, user, title, *, at):
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title=title)
    Session.objects.filter(pk=s.pk).update(created_at=at)
    return s


def _walk(c, query=""):
    seen, cursor, pages = [], None, 0
    while True:
        url = f"/api/canopy-sessions/search?{query}" + (f"&cursor={cursor}" if cursor else "")
        resp = c.get(url)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        seen += [r["title"] for r in body["sessions"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            return seen, pages


# --- T73: the cursor walk ---------------------------------------------------

def test_search_walks_every_session_newest_activity_first():
    user, ws, c = _ctx()
    for i in range(7):
        _web(ws, user, f"s{i}", at=T0 + dt.timedelta(days=i))
    seen, pages = _walk(c, "limit=3")
    assert seen == [f"s{i}" for i in range(6, -1, -1)]   # every one, once, newest first
    assert pages == 3


def test_search_order_is_total_when_activity_ties():
    user, ws, c = _ctx()
    for i in range(5):
        _web(ws, user, f"tie{i}", at=T0)                    # identical activity
    seen, _ = _walk(c, "limit=2")
    assert sorted(seen) == [f"tie{i}" for i in range(5)]   # no dupes, none lost
    assert len(seen) == 5


def test_search_reaches_past_the_list_cap():
    """The bug: the list stops at 500 and ignores offset. The walk does not stop."""
    user, ws, c = _ctx()
    Session.objects.bulk_create([
        Session(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title=f"b{i}")
        for i in range(520)
    ])
    assert len(c.get("/api/canopy-sessions/?state=all&limit=1000").json()) == 500
    seen, _ = _walk(c, "limit=500")
    assert len(seen) == 520 and len(set(seen)) == 520


def test_search_activity_is_the_binding_then_newest_message():
    user, ws, c = _ctx()
    old_web = _web(ws, user, "web-with-recent-message", at=T0)
    Message.objects.create(session=old_web, turn_index=0, role=Message.USER, plaintext="hi")
    Message.objects.filter(session=old_web).update(created_at=T0 + dt.timedelta(days=5))
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="runner")
    RunnerBinding.objects.create(session=disc, runner=_runner(ws, user), session_key="k",
                                 last_interacted_at=T0 + dt.timedelta(days=9),
                                 live_seen_at=timezone.now())
    _web(ws, user, "plain", at=T0 + dt.timedelta(days=2))
    seen, _ = _walk(c, "limit=10")
    assert seen == ["runner", "web-with-recent-message", "plain"]


def test_search_since_until_window():
    user, ws, c = _ctx()
    for i in range(6):
        _web(ws, user, f"d{i}", at=T0 + dt.timedelta(days=i))
    seen, _ = _walk(c, "since=2026-08-02T00:00:00Z&until=2026-08-05T00:00:00Z")
    assert seen == ["d3", "d2", "d1"]                 # [since, until)
    naive, _ = _walk(c, "since=2026-08-05T00:00:00")   # no zone = UTC
    assert naive == ["d5", "d4"]


def test_search_q_matches_title_and_session_key():
    user, ws, c = _ctx()
    _web(ws, user, "Fix the Pagination bug", at=T0)
    _web(ws, user, "unrelated", at=T0)
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="by key")
    RunnerBinding.objects.create(session=disc, runner=_runner(ws, user),
                                 session_key="hal-pagination-1", live_seen_at=timezone.now())
    seen, _ = _walk(c, "q=pagination")
    assert sorted(seen) == ["Fix the Pagination bug", "by key"]   # matched on its session_key


def test_search_repo_matches_project_or_emdash_project():
    user, ws, c = _ctx()
    repo_chat = Session.objects.create(workspace=ws, created_by=user, project="canopy-web", title="repo chat")
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="runner")
    RunnerBinding.objects.create(session=disc, runner=_runner(ws, user), session_key="k",
                                 emdash_project="canopy-web", live_seen_at=timezone.now())
    _web(ws, user, "other", at=T0)
    titles = {r["id"] for r in c.get("/api/canopy-sessions/search?repo=Canopy-Web").json()["sessions"]}
    assert titles == {str(repo_chat.id), str(disc.id)}


def test_search_defaults_to_all_states_and_honours_state():
    user, ws, c = _ctx()
    _web(ws, user, "live", at=T0)
    gone = _web(ws, user, "archived", at=T0)
    Session.objects.filter(pk=gone.pk).update(status=Session.ARCHIVED)
    assert {r["title"] for r in c.get("/api/canopy-sessions/search").json()["sessions"]} == {"live", "archived"}
    assert [r["title"] for r in c.get("/api/canopy-sessions/search?state=active").json()["sessions"]] == ["live"]
    assert c.get("/api/canopy-sessions/search?state=bogus").status_code == 422


def test_search_rejects_a_forged_cursor():
    _user, _ws, c = _ctx()
    assert c.get("/api/canopy-sessions/search?cursor=not-a-cursor").status_code == 422


def test_search_is_tenant_scoped():
    user, ws, c = _ctx()
    _web(ws, user, "mine", at=T0)
    other, ows, _ = _ctx("other")
    _web(ows, other, "theirs", at=T0)
    seen, _ = _walk(c)
    assert seen == ["mine"]


def test_bare_list_shape_is_unchanged_and_takes_the_filters():
    user, ws, c = _ctx()
    _web(ws, user, "alpha", at=T0)
    _web(ws, user, "beta", at=T0 + dt.timedelta(days=3))
    body = c.get("/api/canopy-sessions/").json()
    assert isinstance(body, list) and {r["title"] for r in body} == {"alpha", "beta"}
    assert [r["title"] for r in c.get("/api/canopy-sessions/?q=alp").json()] == ["alpha"]
    assert [r["title"] for r in c.get("/api/canopy-sessions/?since=2026-08-02T00:00:00Z").json()] == ["beta"]


# --- T77: human inputs --------------------------------------------------------

def _msg(s, i, role, text, **kw):
    return Message.objects.create(session=s, turn_index=i, role=role, plaintext=text,
                                  content={"text": text}, **kw)


def test_human_inputs_are_only_what_a_person_typed():
    user, ws, c = _ctx()
    s = _web(ws, user, "chat", at=T0)
    _msg(s, 0, Message.USER, "please fix the list")
    _msg(s, 1, Message.ASSISTANT, "on it")
    _msg(s, 2, Message.TOOL_USE, "Bash")
    _msg(s, 3, Message.TOOL_RESULT, "ok")
    _msg(s, 4, Message.SYSTEM, "compacted")
    _msg(s, 5, Message.USER, "<task-notification> Monitor event: deploy done")   # harness record
    _msg(s, 6, Message.USER, "thanks, ship it")
    body = c.get(f"/api/canopy-sessions/{s.id}/human-inputs").json()
    assert [m["plaintext"] for m in body["messages"]] == ["please fix the list", "thanks, ship it"]
    assert body["next_cursor"] is None
    assert body["source"] == "transcript"


def test_human_inputs_exclude_program_prompts_but_keep_chat_sends():
    user, ws, c = _ctx()
    s = _web(ws, user, "chat", at=T0)
    Turn.objects.create(chat_session=s, origin="canopy_scheduler", prompt="/hal:turn",
                        idempotency_key="sched:1", workspace=ws)
    Turn.objects.create(chat_session=s, origin="canopy_web_chat", prompt="what changed?",
                        idempotency_key=f"chat:{s.id}:1", workspace=ws, initiator_user=user)
    _msg(s, 0, Message.USER, "/hal:turn")
    _msg(s, 1, Message.USER, "what changed?")
    _msg(s, 2, Message.USER, "/hal:turn", author={"name": "JJ", "user_id": user.id})   # a person, by marker
    body = c.get(f"/api/canopy-sessions/{s.id}/human-inputs").json()
    assert [m["turn_index"] for m in body["messages"]] == [1, 2]


def test_human_inputs_page_forward_with_a_cursor():
    user, ws, c = _ctx()
    s = _web(ws, user, "chat", at=T0)
    for i in range(12):
        _msg(s, 2 * i, Message.USER, f"ask {i}")
        _msg(s, 2 * i + 1, Message.TOOL_RESULT, f"result {i}")
    got, after = [], None
    while True:
        url = f"/api/canopy-sessions/{s.id}/human-inputs?limit=5" + (f"&after={after}" if after is not None else "")
        body = c.get(url).json()
        got += [m["plaintext"] for m in body["messages"]]
        after = body["next_cursor"]
        if after is None:
            break
    assert got == [f"ask {i}" for i in range(12)]


def test_human_inputs_fall_back_to_the_runner_tail():
    user, ws, c = _ctx()
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="local")
    RunnerBinding.objects.create(
        session=disc, runner=_runner(ws, user), session_key="k", live_seen_at=timezone.now(),
        tail=[{"role": "user", "text": "hello agent"}, {"role": "assistant", "text": "hi"},
              {"role": "user", "text": "<system-reminder> x"}],
    )
    body = c.get(f"/api/canopy-sessions/{disc.id}/human-inputs").json()
    assert [m["plaintext"] for m in body["messages"]] == ["hello agent"]
    assert body["source"] == "tail" and body["next_cursor"] is None


def test_human_inputs_follow_the_session_access_rule():
    user, ws, c = _ctx()
    s = _web(ws, user, "chat", at=T0)
    _msg(s, 0, Message.USER, "secret")
    _other, _ows, c2 = _ctx("other")
    assert c2.get(f"/api/canopy-sessions/{s.id}/human-inputs").status_code == 404
