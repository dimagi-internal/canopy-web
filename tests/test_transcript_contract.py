"""The server holds incoming transcript rows to the row contract
(`canopy_transcript.contract`), one row at a time.

A batch is one transaction, so a malformed row that reached the insert used to fail
every good row with it. Now each row is judged alone: a bad one is logged and left
out, and the rest of its batch lands. The bar is the server's tolerant one, so what
runners in the field ship (no content, no ordinal) still writes.
"""
import logging

import pytest
from canopy_transcript import contract
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import transcript_ingest
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp", workspace=ws, location=Runner.LOCAL, owner=user,
        host="jj@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, created_by=user,
                               title="ace-demo")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="ace-demo",
                                 thread_key="emdash:ace-demo", host=runner.host,
                                 stream_desired=True)
    c = Client()
    c.force_login(user)
    return runner, s, c


def _rows(session):
    return [(m.turn_index, m.role, m.plaintext) for m in session.messages.order_by("turn_index")]


def test_the_contract_roles_are_the_stored_roles():
    assert contract.ROLES == {value for value, _label in Message.ROLE_CHOICES}


def test_a_malformed_row_is_refused_alone(caplog):
    _r, s, _c = _ctx()
    with caplog.at_level(logging.WARNING, logger="apps.canopy_sessions.transcript_ingest"):
        written = transcript_ingest.persist_transcript_rows(s, [
            {"index": 64, "role": "user", "text": "q1", "content": {"record": {}}},
            {"index": 128, "role": "tool_use", "text": "", "content": {"id": 9, "input": "ls"}},
            # Past the column: this one row used to fail the whole insert.
            {"index": contract.ORDINAL_MAX + 1, "role": "assistant", "text": "too far"},
            {"index": 192, "role": "assistant", "text": "a1"},
        ])
    assert written == 2
    assert _rows(s) == [(64, "user", "q1"), (192, "assistant", "a1")]
    refused = [r.getMessage() for r in caplog.records if "transcript row refused" in r.getMessage()]
    assert len(refused) == 2
    assert "index=128" in refused[0] and "content.id must be a string" in refused[0]


def test_what_old_runners_ship_still_writes():
    _r, s, _c = _ctx()
    # No ordinal (an old runner): numbered by the server, into an empty session, as before.
    assert transcript_ingest.write_backfill(s, [{"role": "assistant", "text": "x", "index": -1}]) == 1
    assert transcript_ingest.persist_transcript_rows(s, [
        {"index": 5, "role": "user", "text": "no content key"},
        {"index": 7, "role": "tool_use", "text": "", "content": {}},
    ]) == 2


def test_a_row_shifted_past_the_column_is_refused_alone():
    _r, s, _c = _ctx()
    binding = s.runner_binding
    binding.index_offset = contract.ORDINAL_MAX - 100
    binding.save(update_fields=["index_offset"])
    assert transcript_ingest.persist_transcript_rows(s, [
        {"index": 50, "role": "user", "text": "fits"},
        {"index": 200, "role": "assistant", "text": "does not"},
    ]) == 1
    assert [m.plaintext for m in s.messages.all()] == ["fits"]


def test_the_stream_neither_stores_nor_shows_a_malformed_row(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    runner, s, c = _ctx()
    body = c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(s.id), "events": [
            {"kind": "user", "seq": 5, "index": 5, "payload": {"text": "q1"}},
            {"kind": "tool_result", "seq": 6, "index": 6,
             "payload": {"text": "x", "tool_use_id": "t", "is_error": "no"}},
            {"kind": "activity:working", "seq": -1, "index": -1, "payload": {}},
            {"kind": "assistant", "seq": 7, "index": 7, "payload": {"text": "a1"}},
        ]},
        content_type="application/json",
    ).json()
    assert _rows(s) == [(5, "user", "q1"), (7, "assistant", "a1")]
    assert body == {"count": 3}
    assert [m["event"]["kind"] for _g, m in published] == ["user", "activity:working", "assistant"]
