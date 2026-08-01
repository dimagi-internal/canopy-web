import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, paired_by=user)
    c = Client(); c.force_login(user)
    return user, ws, runner, c


def test_streams_lists_every_binding_and_flags_which_are_watched():
    """All backed sessions, watched or not — `live` only governs fan-out.

    This deliberately replaces "only desired bindings". While the list was scoped
    to attached sessions, transcript rows reached the server only for a session
    someone had open, and only from the moment they opened it: labs held 983 of
    6119 rows across 12 live sessions (16%), 8 of them at exactly zero. Durability
    must not be a side effect of being looked at, so the runner tails everything
    and `live` decides only whether rows are also pushed to watching clients.
    """
    user, ws, runner, c = _ctx()
    s1 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, project="echo", title="a")
    s2 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="b")
    RunnerBinding.objects.create(session=s1, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    RunnerBinding.objects.create(session=s2, runner=runner, session_key="echo-2",
                                 stream_desired=False)  # unwatched -> still persisted
    body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
    by_key = {x["session_key"]: x for x in body["streams"]}
    assert sorted(by_key) == ["echo-1", "echo-2"]
    assert by_key["echo-1"]["session_id"] == str(s1.id)
    assert by_key["echo-1"]["project"] == "echo"
    assert by_key["echo-1"]["live"] is True
    assert by_key["echo-2"]["live"] is False


def test_streams_reports_both_bounds_of_what_the_server_holds():
    """`first_index` as well as `last_index`, because a max alone cannot express
    "you are missing the beginning" — and rows can only be appended above the
    high-water mark, so a session whose head was never captured could otherwise
    never repair itself (labs had one pinned at 8.6%)."""
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1")
    empty = c.get(f"/api/harness/runners/{runner.id}/streams").json()["streams"][0]
    assert empty["first_index"] is None and empty["last_index"] is None
    Message.objects.create(session=s, turn_index=448, role="assistant", plaintext="a")
    Message.objects.create(session=s, turn_index=1024, role="assistant", plaintext="b")
    held = c.get(f"/api/harness/runners/{runner.id}/streams").json()["streams"][0]
    assert (held["first_index"], held["last_index"]) == (448, 1024)


def test_session_stream_publishes_stream_frames(monkeypatch):
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1", stream_desired=True)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    body = c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(s.id),
              "events": [{"kind": "assistant", "seq": 0, "payload": {"text": "hi"}}]},
        content_type="application/json",
    ).json()
    assert body == {"count": 1}
    assert len(published) == 1
    group, frame = published[0]
    assert group.endswith(s.id.hex)                 # the session group
    assert frame["type"] == "chat.turn_event"
    assert frame["turn_id"] is None                 # turn-less live frame
    assert frame["event"] == {"kind": "assistant", "seq": 0, "payload": {"text": "hi"}}


def test_session_stream_rejects_unbound_runner():
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    # binding belongs to a DIFFERENT runner
    other = Runner.objects.create(name="other", workspace=ws, location=Runner.LOCAL, paired_by=user)
    RunnerBinding.objects.create(session=s, runner=other, session_key="echo-1")
    resp = c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(s.id), "events": []},
        content_type="application/json",
    )
    assert resp.status_code == 404


# --- Tool calls over the stream (tier 1 of live-execution visibility) --------


def _post_stream(c, runner, session, events):
    return c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(session.id), "events": events},
        content_type="application/json",
    )


def test_tool_rows_persist_with_the_identity_the_ui_pairs_on():
    """The payload IS the stored content. Flattening it to text (the old
    behaviour) stripped `id`/`name`/`input` and `tool_use_id` — i.e. exactly the
    fields `pairToolMessages` needs to render a call and its result as one row."""
    from apps.canopy_sessions.models import Message

    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    _post_stream(c, runner, s, [
        {"kind": "tool_use", "seq": 64, "index": 64,
         "payload": {"id": "toolu_1", "name": "Bash",
                     "input": {"command": "ls"}, "text": ""}},
        {"kind": "tool_result", "seq": 128, "index": 128,
         "payload": {"tool_use_id": "toolu_1", "is_error": False, "text": "a.txt"}},
    ])

    use = Message.objects.get(session=s, turn_index=64)
    assert use.role == Message.TOOL_USE
    assert use.content["id"] == "toolu_1"
    assert use.content["name"] == "Bash"
    assert use.content["input"] == {"command": "ls"}

    result = Message.objects.get(session=s, turn_index=128)
    assert result.role == Message.TOOL_RESULT
    assert result.content["tool_use_id"] == "toolu_1"
    assert result.plaintext == "a.txt"


def test_tool_frames_reach_the_client_live(monkeypatch):
    """A tool row is only useful while the agent is working, so it has to fan out
    live — not merely persist for the next reload."""
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    _post_stream(c, runner, s, [
        {"kind": "tool_use", "seq": 64, "index": 64,
         "payload": {"id": "toolu_1", "name": "Bash", "input": {}, "text": ""}},
    ])
    assert [m["event"]["kind"] for m in published] == ["tool_use"]


def test_plain_text_rows_store_no_redundant_content():
    """An older runner posts {"text": ...} and nothing else. That whole payload is
    a duplicate of plaintext, so the stored content is empty — see
    services.storage_content. The row itself is unchanged."""
    from apps.canopy_sessions.models import Message

    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    _post_stream(c, runner, s, [
        {"kind": "assistant", "seq": 64, "index": 64, "payload": {"text": "hello"}},
    ])
    msg = Message.objects.get(session=s, turn_index=64)
    assert msg.plaintext == "hello"
    assert msg.content == {}


def test_user_events_are_fanned_out_live(monkeypatch):
    """They used to be withheld because "the sender's client already echoed
    them" — true for the web, false for emdash, where nothing echoed. The result
    was that typing in emdash and watching on the phone dropped your own words
    until a reload."""
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    _post_stream(c, runner, s, [
        {"kind": "user", "seq": 64, "index": 64, "payload": {"text": "typed in emdash"}},
    ])
    assert [m["event"]["kind"] for m in published] == ["user"]


def test_harness_markers_are_not_pushed_live_either(monkeypatch):
    """The noise filter used to live only on the durable path. Once user events
    started fanning out, a marker would appear live and then VANISH on reload —
    filtered from history but not from the stream. Same rule, both paths."""
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    _post_stream(c, runner, s, [
        {"kind": "user", "seq": 64, "index": 64,
         "payload": {"text": "[Request interrupted by user]"}},
        {"kind": "user", "seq": 128, "index": 128,
         "payload": {"text": "something I actually typed"}},
    ])
    texts = [m["event"]["payload"]["text"] for m in published]
    assert texts == ["something I actually typed"]
    # And it is absent from history too, so the two agree.
    assert [m.plaintext for m in s.messages.all()] == ["something I actually typed"]


def test_activity_events_fan_out_and_never_persist(monkeypatch):
    """A turn boundary is a state transition, not a chat row: it must reach every
    watching client and leave no trace in the transcript."""
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    _post_stream(c, runner, s, [
        {"kind": "activity:working", "seq": -1, "index": -1, "payload": {}},
    ])
    assert [m["event"]["kind"] for m in published] == ["activity:working"]
    assert s.messages.count() == 0


def test_streams_excludes_archived_sessions():
    """Eager tailing must not sweep in every session the box ever held.

    Widening the list from `stream_desired` to "everything" would otherwise
    re-ship the full history of every retired conversation on each runner
    restart — labs accumulated 71 sessions at one point — for transcripts that
    are not growing and nobody is reading. `drain_backfills` is a separate path
    this filter does not touch, so an explicit "Load full session" on an archived
    session still works: eager for live, on demand for retired.
    """
    user, ws, runner, c = _ctx()
    live = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    gone = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="b",
                                  status=Session.ARCHIVED)
    RunnerBinding.objects.create(session=live, runner=runner, session_key="live-1")
    RunnerBinding.objects.create(session=gone, runner=runner, session_key="gone-1")
    body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
    assert [x["session_key"] for x in body["streams"]] == ["live-1"]
