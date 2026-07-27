import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import RunnerBinding, Session
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


def test_streams_lists_only_desired_bindings():
    user, ws, runner, c = _ctx()
    s1 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, project="echo", title="a")
    s2 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="b")
    RunnerBinding.objects.create(session=s1, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    RunnerBinding.objects.create(session=s2, runner=runner, session_key="echo-2",
                                 stream_desired=False)  # not attached -> excluded
    body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
    assert [x["session_key"] for x in body["streams"]] == ["echo-1"]
    assert body["streams"][0]["session_id"] == str(s1.id)
    assert body["streams"][0]["project"] == "echo"


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
