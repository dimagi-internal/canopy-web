"""Chat attachments — upload, read back, and discard before sending.

The bytes live in S3; these tests stub that boundary so the contract under test
is the API's, not boto3's. What matters here is the gating (tenant, type, size),
the unbound-then-bound lifecycle, and the order of writes.
"""
from __future__ import annotations

import uuid
from io import BytesIO
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Attachment, Message, Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
STORAGE = "apps.canopy_sessions.attachment_storage"


@pytest.fixture(autouse=True)
def _bucket(settings):
    settings.CANOPY_ATTACHMENTS_BUCKET = "test-bucket"
    return settings


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    session = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB)
    c = Client()
    c.force_login(user)
    return user, ws, session, c


def _upload(client, session_id, *, name="shot.png", data=PNG, content_type="image/png"):
    handle = BytesIO(data)
    handle.name = name
    with mock.patch(f"{STORAGE}.put") as put:
        resp = client.post(
            f"/api/canopy-sessions/{session_id}/attachments",
            {"file": handle},
        )
    return resp, put


def test_upload_stores_the_bytes_and_returns_an_id():
    _user, _ws, session, c = _ctx()

    resp, put = _upload(c, session.id)

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["filename"] == "shot.png"
    assert body["size_bytes"] == len(PNG)
    assert body["message_id"] is None, "unbound until the message is sent"

    row = Attachment.objects.get(pk=body["id"])
    assert row.session_id == session.id
    put.assert_called_once()
    key, sent_bytes, content_type = put.call_args[0]
    assert key == row.storage_key
    assert sent_bytes == PNG
    assert content_type == "image/png"


def test_the_key_is_session_prefixed_and_unique_per_attachment():
    """Two screenshots really are both called IMG_0001.png."""
    _user, _ws, session, c = _ctx()

    first = _upload(c, session.id, name="IMG_0001.png")[0].json()
    second = _upload(c, session.id, name="IMG_0001.png")[0].json()

    a, b = Attachment.objects.get(pk=first["id"]), Attachment.objects.get(pk=second["id"])
    assert a.storage_key != b.storage_key
    assert a.storage_key.startswith(f"sessions/{session.id}/")
    assert b.storage_key.startswith(f"sessions/{session.id}/")


def test_a_path_traversal_filename_cannot_escape_the_session_prefix():
    _user, _ws, session, c = _ctx()

    body = _upload(c, session.id, name="../../../../etc/passwd.png")[0].json()

    row = Attachment.objects.get(pk=body["id"])
    assert row.storage_key.startswith(f"sessions/{session.id}/")
    assert ".." not in row.storage_key


def test_a_non_image_is_refused():
    # Allowlist, not denylist: an agent opens these and a browser renders them.
    _user, _ws, session, c = _ctx()

    resp, put = _upload(c, session.id, name="x.pdf", content_type="application/pdf",
                        data=b"%PDF-1.4")

    assert resp.status_code == 422
    put.assert_not_called()
    assert Attachment.objects.count() == 0


def test_a_file_over_the_cap_is_refused(settings):
    settings.ATTACHMENT_MAX_UPLOAD_BYTES = 100
    _user, _ws, session, c = _ctx()

    resp, put = _upload(c, session.id, data=b"0" * 500)

    assert resp.status_code == 422
    put.assert_not_called()
    assert Attachment.objects.count() == 0


def test_upload_503s_when_no_bucket_is_configured(settings):
    """Loud, not silently degraded: a row pointing at bytes that were never
    stored is a broken thumbnail and a runner download that 500s."""
    settings.CANOPY_ATTACHMENTS_BUCKET = ""
    _user, _ws, session, c = _ctx()

    resp, _put = _upload(c, session.id)

    assert resp.status_code == 503
    assert Attachment.objects.count() == 0


def test_a_non_member_cannot_upload_to_someone_elses_session():
    _user, _ws, session, _c = _ctx()
    stranger = Client()
    stranger.force_login(User.objects.create_user("s", "s@example.org", "pw"))

    resp, put = _upload(stranger, session.id)

    assert resp.status_code == 404  # no existence leak
    put.assert_not_called()


# --- reading back -----------------------------------------------------------


def _make(session, user, **kw):
    return Attachment.objects.create(
        session=session, uploaded_by=user, filename="shot.png",
        content_type="image/png", size_bytes=len(PNG),
        storage_key=f"sessions/{session.id}/{uuid.uuid4()}/shot.png", **kw,
    )


def test_content_streams_the_bytes_inline():
    user, _ws, session, c = _ctx()
    row = _make(session, user)

    with mock.patch(f"{STORAGE}.get") as get:
        get.return_value = mock.Mock(body=PNG, content_type="image/png")
        resp = c.get(f"/api/canopy-sessions/attachments/{row.id}/content")

    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp["Content-Type"] == "image/png"
    assert "inline" in resp["Content-Disposition"]


def test_a_teammate_can_read_what_was_shared_in_the_session():
    """Gated on session membership, not on who uploaded — a session is
    multiplayer, so the other person must see the screenshot."""
    user, ws, session, _c = _ctx()
    row = _make(session, user)
    mate = User.objects.create_user("mate", "mate@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=mate, workspace=ws, role=WorkspaceMembership.EDITOR)
    mate_client = Client()
    mate_client.force_login(mate)

    with mock.patch(f"{STORAGE}.get") as get:
        get.return_value = mock.Mock(body=PNG, content_type="image/png")
        resp = mate_client.get(f"/api/canopy-sessions/attachments/{row.id}/content")

    assert resp.status_code == 200


def test_a_stranger_cannot_read_an_attachment():
    user, _ws, session, _c = _ctx()
    row = _make(session, user)
    stranger = Client()
    stranger.force_login(User.objects.create_user("s", "s@example.org", "pw"))

    with mock.patch(f"{STORAGE}.get") as get:
        resp = stranger.get(f"/api/canopy-sessions/attachments/{row.id}/content")

    assert resp.status_code == 404
    get.assert_not_called()


# --- discarding -------------------------------------------------------------


def test_an_unsent_attachment_can_be_discarded():
    user, _ws, session, c = _ctx()
    row = _make(session, user)

    with mock.patch(f"{STORAGE}.delete") as delete:
        resp = c.delete(f"/api/canopy-sessions/attachments/{row.id}")

    assert resp.status_code == 204
    delete.assert_called_once_with(row.storage_key)
    assert not Attachment.objects.filter(pk=row.id).exists()


def test_a_sent_attachment_cannot_be_discarded():
    """Once it is part of a sent message it is transcript — deleting it would
    leave the agent's reply referring to something nobody else can see."""
    user, _ws, session, c = _ctx()
    message = Message.objects.create(
        session=session, turn_index=0, role=Message.USER, plaintext="look", content={},
    )
    row = _make(session, user, message=message)

    with mock.patch(f"{STORAGE}.delete") as delete:
        resp = c.delete(f"/api/canopy-sessions/attachments/{row.id}")

    assert resp.status_code == 409
    delete.assert_not_called()
    assert Attachment.objects.filter(pk=row.id).exists()


def test_deleting_a_message_unbinds_rather_than_destroys_the_attachment():
    """SET_NULL, not CASCADE: losing the message must not silently destroy bytes
    the agent may still be reading."""
    user, _ws, session, _c = _ctx()
    message = Message.objects.create(
        session=session, turn_index=0, role=Message.USER, plaintext="look", content={},
    )
    row = _make(session, user, message=message)

    message.delete()

    row.refresh_from_db()
    assert row.message_id is None


# --- delivery: attachments ride the turn to the runner ----------------------


def _pending(session, user, name="shot.png"):
    return Attachment.objects.create(
        session=session, uploaded_by=user, filename=name,
        content_type="image/png", size_bytes=len(PNG),
        storage_key=f"sessions/{session.id}/{uuid.uuid4()}/{name}",
    )


def test_sending_carries_pending_attachments_to_the_runner():
    from apps.canopy_sessions import services

    user, _ws, session, _c = _ctx()
    _pending(session, user, "shot.png")

    _msg, turn = services.send_message(session=session, text="what is this?", user=user)

    refs = turn.origin_ref.get("attachments")
    assert refs and [r["filename"] for r in refs] == ["shot.png"]
    assert "id" in refs[0] and "content_type" in refs[0]


def test_a_sent_attachment_does_not_ride_along_on_the_next_send():
    """sent_at is what stops this. `message` alone cannot: a runner-origin
    session writes no user Message row, so those rows stay message=NULL."""
    from apps.canopy_sessions import services

    user, _ws, session, _c = _ctx()
    _pending(session, user)

    services.send_message(session=session, text="first", user=user)
    _msg2, turn2 = services.send_message(session=session, text="second", user=user, client_id="c2")

    assert "attachments" not in turn2.origin_ref


def test_a_send_with_nothing_attached_adds_no_key():
    from apps.canopy_sessions import services

    user, _ws, session, _c = _ctx()
    _msg, turn = services.send_message(session=session, text="hi", user=user)
    assert "attachments" not in turn.origin_ref


def test_sending_binds_the_attachment_to_the_message_on_a_web_session():
    from apps.canopy_sessions import services

    user, _ws, session, _c = _ctx()
    row = _pending(session, user)

    message, _turn = services.send_message(session=session, text="look", user=user)

    row.refresh_from_db()
    assert row.message_id == message.pk
    assert row.sent_at is not None


def test_a_runner_session_stamps_sent_at_without_a_message():
    """The runner path writes no durable user row, so message stays NULL — but
    the attachment must still be consumed."""
    from apps.canopy_sessions import services

    user, ws, _s, _c = _ctx()
    runner_session = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="ddd",
    )
    row = _pending(runner_session, user)

    _msg, turn = services.send_message(session=runner_session, text="look", user=user)

    row.refresh_from_db()
    assert row.sent_at is not None
    assert row.message_id is None
    assert [r["filename"] for r in turn.origin_ref["attachments"]] == ["shot.png"]
