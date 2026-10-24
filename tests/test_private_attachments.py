"""Files in a private (sensitive) conversation: the agent's copy for one turn, the
people's sealed (`apps/canopy_sessions/private_attachments.py`).

Every test goes through the real door: the upload and sealed-copy routes a person's
browser calls, the send, the runner's download route, `close_turn`, the deadline
sweep, the history routes. The test stands in for the browser where a browser seals
or opens (`_seal`, `_open`) — never app code.

docs/architecture/tenancy.md § Sensitive conversations ("Files").
"""
from __future__ import annotations

import json
import logging
import struct
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.agents.interface import parse
from apps.canopy_sessions import conversation_keys, private_attachments
from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
from apps.canopy_sessions.models import Attachment, Message, SessionKey, SessionKeyWrap, SessionParticipant
from apps.common import ecdh_es
from apps.harness.closing import close_turn
from apps.harness.models import Runner, Turn, TurnEvent
from apps.harness.sweeps import expire_unrun_sensitive_turns
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.sensitive import minted, unwrap, vault_key, world

pytestmark = pytest.mark.django_db

STORAGE = "apps.canopy_sessions.attachment_storage"
NAME = "amina-hiv-results.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"amina-pixels" * 40


class FakeBucket:
    """S3, in memory: what is put, got and deleted."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.deleted: list[str] = []

    def put(self, key, body, content_type):
        self.objects[key] = (body, content_type)

    def get(self, key):
        body, ct = self.objects[key]
        return mock.Mock(body=body, content_type=ct)

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.fixture()
def bucket(settings):
    settings.CANOPY_ATTACHMENTS_BUCKET = "test-bucket"
    settings.ATTACHMENT_ALLOWED_CONTENT_TYPES = ["image/png"]
    settings.CHAT_STUB_EXECUTOR = False  # a runner runs the turn, as in production
    b = FakeBucket()
    with mock.patch(f"{STORAGE}.put", side_effect=b.put), \
            mock.patch(f"{STORAGE}.get", side_effect=b.get), \
            mock.patch(f"{STORAGE}.delete", side_effect=b.delete):
        yield b


@pytest.fixture()
def w():
    out = world()
    # A private conversation runs only in a capability that handles sensitive data.
    out["agent"].interface = parse({"capabilities": {"data": {
        "callers": ["member"], "sites": ["connect-labs"], "data": "sensitive", "tools": ["Read"]}}})
    out["agent"].save(update_fields=["interface"])
    out["hot"].metadata = {**out["hot"].metadata, "embed_app": "connect-labs"}
    out["hot"].save(update_fields=["metadata"])
    for s in (out["hot"], out["cold"]):
        SessionParticipant.objects.create(session=s, user=out["user"], role=SessionParticipant.OWNER)
    return out


# ── the browser's half, stood in for ─────────────────────────────────────────────


def _seal(session, attachment_id, uploader_id, name=NAME, body=PNG, *, conversation=None):
    """What the uploader's browser sends (`sealedFiles.ts::sealAttachment`): the name
    and bytes sealed to the conversation's public key, bound to this attachment."""
    key = SessionKey.objects.get(session=session)
    c = str(conversation or key.conversation)
    a, u = str(attachment_id), str(uploader_id)
    head = ecdh_es.wrap(json.dumps({"name": name, "type": "image/png", "size": len(body)}).encode(),
                        key.public_jwk, private_attachments.name_info(c, a, u))
    sealed_name = private_attachments.NAME_PREFIX + ecdh_es.b64u(
        json.dumps({"c": c, "a": a, "u": u, **head}).encode())
    wrapped = ecdh_es.wrap(body, key.public_jwk, private_attachments.blob_info(c, a, u))
    header = json.dumps({"c": c, "a": a, "u": u, "alg": wrapped["alg"], "epk": wrapped["epk"],
                         "n": wrapped["n"]}).encode()
    blob = private_attachments.BLOB_MAGIC + struct.pack(">I", len(header)) + header + ecdh_es.unb64u(wrapped["ct"])
    return sealed_name, blob


def _open(session, person, priv, sealed_name, blob):
    """What a member's browser does with the history row and the sealed bytes: unwrap
    its copy of the conversation key, then open the name and the file."""
    key = SessionKey.objects.get(session=session)
    wrap = SessionKeyWrap.objects.get(session=session, user=person)
    scalar = unwrap(wrap.wrap, priv, conversation_keys.wrap_info(key.conversation, person.pk))
    conv = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    body = json.loads(ecdh_es.unb64u(sealed_name[len(private_attachments.NAME_PREFIX):]))
    meta = json.loads(unwrap(body, conv, private_attachments.name_info(body["c"], body["a"], body["u"])))
    (n,) = struct.unpack(">I", blob[4:8])
    header = json.loads(blob[8:8 + n])
    raw = unwrap({**header, "ct": ecdh_es.b64u(blob[8 + n:])}, conv,
                 private_attachments.blob_info(header["c"], header["a"], header["u"]))
    return meta, raw


def _upload(client, session, name=NAME, body=PNG):
    return client.post(f"/api/canopy-sessions/{session.pk}/attachments",
                       {"file": SimpleUploadedFile(name, body, "image/png")})


def _post_sealed(client, attachment_id, sealed_name, blob):
    return client.post(f"/api/canopy-sessions/attachments/{attachment_id}/sealed",
                       {"sealed_name": sealed_name, "file": SimpleUploadedFile("blob", blob)})


def _attach_sealed(w, session=None):
    session = session or w["hot"]
    up = _upload(w["c"], session)
    assert up.status_code == 201, up.content
    aid = up.json()["id"]
    sealed_name, blob = _seal(session, aid, w["user"].pk)
    r = _post_sealed(w["c"], aid, sealed_name, blob)
    assert r.status_code == 204, r.content
    return Attachment.objects.get(pk=aid)


def _send(w, session=None, text="what do these results say?"):
    session = session or w["hot"]
    r = w["c"].post(f"/api/canopy-sessions/{session.pk}/send", {"text": text},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    return Turn.objects.filter(chat_session=session).latest("created_at")


def _box(w, name="zdr"):
    return Runner.objects.create(name=name, workspace=w["ws"], kind=Runner.CLOUD, status=Runner.ONLINE,
                                 owner=w["user"], last_heartbeat_at=timezone.now())


def _fetch(runner, turn, attachment_id):
    c = Client()
    c.force_login(runner.owner)
    return c.get(f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/attachments/{attachment_id}/content")


def _claim(turn, runner):
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, claimed_by=runner)
    turn.refresh_from_db()
    return turn


# ── 1. the agent's copy is held for one turn ─────────────────────────────────────


def test_a_file_in_a_private_conversation_is_stored_under_no_name(w, bucket):
    up = _upload(w["c"], w["hot"])
    assert up.status_code == 201, up.content
    row = Attachment.objects.get(pk=up.json()["id"])
    assert row.sensitive and row.storage_key == private_attachments.plain_key(w["hot"].pk, row.pk)
    assert all("amina" not in k for k in bucket.objects)
    # A bucket and a family, never the file's own size and type.
    assert row.size_bytes == 64 * 1024 and row.content_type == "image/*"


def test_a_file_with_no_sealed_copy_is_not_sent(w, bucket):
    up = _upload(w["c"], w["hot"])
    turn = _send(w)
    assert not (turn.origin_ref or {}).get("attachments")
    assert Attachment.objects.get(pk=up.json()["id"]).sent_at is None


def test_the_turn_names_the_file_by_id_alone_and_its_box_fetches_it_once(w, bucket, django_capture_on_commit_callbacks):
    row = _attach_sealed(w)
    turn = _send(w)
    assert turn.origin_ref["attachments"] == [{"id": str(row.pk), "sealed": True}]
    assert NAME not in json.dumps(turn.origin_ref)
    row.refresh_from_db()
    assert row.turn_id == turn.pk
    box = _box(w)
    _claim(turn, box)

    with django_capture_on_commit_callbacks(execute=True):
        first = _fetch(box, turn, row.pk)
    assert first.status_code == 200 and first.content == PNG
    assert NAME in first["Content-Disposition"] and first["Cache-Control"] == "no-store"
    # Gone the moment it was served: the object, the name, the key.
    assert private_attachments.plain_key(w["hot"].pk, row.pk) in bucket.deleted
    row.refresh_from_db()
    assert row.filename == "" and row.storage_key == ""
    assert _fetch(box, turn, row.pk).status_code == 404
    # The people's copy stays.
    assert row.sealed_blob_key in bucket.objects


def test_only_the_box_holding_the_claiming_turn_gets_it(w, bucket):
    row = _attach_sealed(w)
    turn = _send(w)
    box, other = _box(w), _box(w, "other")
    # Not while it is queued, nor by a box that does not hold it.
    assert _fetch(box, turn, row.pk).status_code == 404
    _claim(turn, box)
    assert _fetch(other, turn, row.pk).status_code == 404
    # Not through a turn of another conversation this box holds.
    elsewhere = Turn.objects.create(chat_session=w["cold"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="q",
                                    idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=box)
    assert _fetch(box, elsewhere, row.pk).status_code == 404
    # Never to a person, at a browser or through a token.
    assert w["c"].get(f"/api/canopy-sessions/attachments/{row.pk}/content").status_code == 404
    assert minted(w["user"]).get(f"/api/canopy-sessions/attachments/{row.pk}/content").status_code == 404
    # The box that holds it still does.
    assert _fetch(box, turn, row.pk).status_code == 200


def test_a_turn_that_ends_before_its_box_fetched_the_file_takes_it_with_it(
        w, bucket, django_capture_on_commit_callbacks):
    row = _attach_sealed(w)
    turn = _claim(_send(w), _box(w))
    plain = row.storage_key
    with django_capture_on_commit_callbacks(execute=True):
        assert close_turn(turn, Turn.FAILED, "boom", from_states=[Turn.CLAIMED])
    row.refresh_from_db()
    assert plain in bucket.deleted and row.storage_key == "" and row.filename == ""
    assert row.sealed_blob_key in bucket.objects


def test_an_unrun_turn_past_its_deadline_takes_its_file_with_it(w, bucket, django_capture_on_commit_callbacks):
    row = _attach_sealed(w)
    turn = _send(w)
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - timedelta(hours=2))
    assert turn.status == Turn.QUEUED
    with django_capture_on_commit_callbacks(execute=True):
        assert expire_unrun_sensitive_turns() == 1
    row.refresh_from_db()
    assert row.storage_key == "" and row.filename == ""
    assert private_attachments.plain_key(w["hot"].pk, row.pk) in bucket.deleted


def test_the_deadline_sweep_takes_an_unsent_file_whole_and_an_orphaned_agent_copy(
        w, bucket, django_capture_on_commit_callbacks):
    unsent = _attach_sealed(w)
    sent = _attach_sealed(w)
    Attachment.objects.filter(pk=sent.pk).update(sent_at=timezone.now() - timedelta(hours=2))
    Attachment.objects.filter(pk=unsent.pk).update(created_at=timezone.now() - timedelta(hours=2))
    with django_capture_on_commit_callbacks(execute=True):
        assert private_attachments.sweep() == 2
    assert not Attachment.objects.filter(pk=unsent.pk).exists()
    assert {unsent.storage_key, unsent.sealed_blob_key} <= set(bucket.deleted)
    sent.refresh_from_db()
    assert sent.storage_key == "" and sent.sealed_blob_key in bucket.objects


# ── 2. the people's copy is sealed ───────────────────────────────────────────────


def test_a_member_opens_the_sealed_copy_and_nobody_else_fetches_it(w, bucket):
    row = _attach_sealed(w)
    _send(w)
    history = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}/sealed-attachments")
    assert history.status_code == 200, history.content
    [entry] = history.json()
    assert "amina" not in json.dumps(entry) and entry["size_bucket"] == 64 * 1024
    got = w["c"].get(f"/api/canopy-sessions/attachments/{row.pk}/sealed")
    assert got.status_code == 200 and b"amina" not in got.content
    meta, raw = _open(w["hot"], w["user"], w["key"], entry["sealed_name"], got.content)
    assert meta["name"] == NAME and raw == PNG

    # A workspace member who is not in it, a token, a minted session: nothing.
    other = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    vault_key(other)
    oc = Client()
    oc.force_login(other)
    from apps.tokens.models import PersonalToken

    raw_pat, _ = PersonalToken.create_for_user(user=w["user"], label="cli")
    for reader in (oc, minted(w["user"]), Client(HTTP_AUTHORIZATION=f"Bearer {raw_pat}")):
        assert reader.get(f"/api/canopy-sessions/attachments/{row.pk}/sealed").status_code == 404
        listed = reader.get(f"/api/canopy-sessions/{w['hot'].pk}/sealed-attachments")
        assert listed.status_code == 404 or listed.json() == []


def test_a_sealed_copy_is_taken_once_from_its_uploader_sealed_for_this_file(w, bucket):
    up = _upload(w["c"], w["hot"])
    aid = up.json()["id"]
    # Sealed for another file, or another conversation: refused.
    wrong, blob = _seal(w["hot"], uuid.uuid4(), w["user"].pk)
    assert _post_sealed(w["c"], aid, wrong, blob).status_code == 422
    name, other_conv = _seal(w["hot"], aid, w["user"].pk, conversation=uuid.uuid4())
    assert _post_sealed(w["c"], aid, name, other_conv).status_code == 422
    # Someone else in it may not store the uploader's.
    peer = User.objects.create_user("pat", "pat@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=peer, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    SessionParticipant.objects.create(session=w["hot"], user=peer, role=SessionParticipant.EDITOR)
    pc = Client()
    pc.force_login(peer)
    good_name, good_blob = _seal(w["hot"], aid, w["user"].pk)
    assert _post_sealed(pc, aid, good_name, good_blob).status_code == 403
    assert _post_sealed(w["c"], aid, good_name, good_blob).status_code == 204
    assert _post_sealed(w["c"], aid, good_name, good_blob).status_code == 409


def test_a_file_is_attached_only_from_its_peoples_browser(w, bucket):
    assert _upload(minted(w["user"]), w["hot"]).status_code == 422
    assert not Attachment.objects.filter(session=w["hot"]).exists()


# ── 3. no name anywhere else ─────────────────────────────────────────────────────


def test_no_name_in_the_turn_its_events_its_transcript_or_a_log(w, bucket, caplog, django_capture_on_commit_callbacks):
    caplog.set_level(logging.DEBUG)
    row = _attach_sealed(w)
    turn = _claim(_send(w), _box(w))
    with django_capture_on_commit_callbacks(execute=True):
        assert _fetch(turn.claimed_by, turn, row.pk).status_code == 200
    # The transcript the box ships back: its note names the file it read, by path.
    path = f"/var/canopy/sessions/{w['hot'].pk}/.canopy-attachments/{turn.pk}/1-{NAME}"
    chat_transcript_ingest.persist_transcript_rows(w["hot"], [
        {"index": 0, "role": "user",
         "text": f"what do these results say?\n\nThe user attached the following file. Read it "
                 f"with the Read tool before replying:\n- {path}"},
        {"index": 64, "role": "tool_use", "text": "",
         "content": {"id": "tu1", "name": "Read", "input": {"file_path": path}}},
        {"index": 65, "role": "tool_result", "text": "amina-pixels",
         "content": {"tool_use_id": "tu1", "is_error": False, "text": "amina-pixels"}},
    ])
    with django_capture_on_commit_callbacks(execute=True):
        close_turn(turn, Turn.DONE, None, from_states=[Turn.CLAIMED])
    turn.refresh_from_db()
    stored = json.dumps([turn.origin_ref, turn.prompt, turn.result_note,
                         list(TurnEvent.objects.filter(turn=turn).values_list("payload", flat=True)),
                         list(Message.objects.filter(session=w["hot"]).values_list("plaintext", "content")),
                         list(Attachment.objects.filter(pk=row.pk).values())], default=str)
    assert "amina" not in stored.lower()
    assert "amina" not in caplog.text.lower()


def test_a_private_conversations_file_is_listed_nowhere_by_name(w, bucket):
    _attach_sealed(w)
    _send(w)
    for url in (f"/api/canopy-sessions/{w['hot'].pk}", f"/api/canopy-sessions/{w['hot'].pk}/messages",
                "/api/canopy-sessions/", f"/api/canopy-sessions/{w['hot'].pk}/info"):
        r = w["c"].get(url)
        assert "amina" not in r.content.decode().lower(), url


# ── 4. deleting the conversation takes both copies ───────────────────────────────


def test_deleting_the_row_takes_both_objects(w, bucket, django_capture_on_commit_callbacks):
    row = _attach_sealed(w)
    with django_capture_on_commit_callbacks(execute=True):
        w["hot"].delete()
    assert {row.storage_key, row.sealed_blob_key} <= set(bucket.deleted)


def test_a_later_turn_never_reaches_an_earlier_turns_file(w, bucket, django_capture_on_commit_callbacks):
    row = _attach_sealed(w)
    box = _box(w)
    first = _claim(_send(w), box)
    with django_capture_on_commit_callbacks(execute=True):
        close_turn(first, Turn.DONE, None, from_states=[Turn.CLAIMED])
    second = _claim(_send(w, text="and now?"), box)
    assert not (second.origin_ref or {}).get("attachments")
    assert _fetch(box, second, row.pk).status_code == 404
    assert _fetch(box, first, row.pk).status_code == 404


# ── the bytes both languages agree on ────────────────────────────────────────────

VECTORS = __import__("pathlib").Path(__file__).resolve().parent.parent / "docs/vault/chat-v1/file_v1_vectors.json"


def _vector() -> dict:
    """Fixed keys and randomness through the browser's construction (stood in for here):
    `frontend/src/vault/sealedFiles.test.ts` opens it byte for byte."""
    def priv(hex32):
        return ec.derive_private_key(int(hex32, 16), ec.SECP256R1())

    c, a, u = "0b7c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3", "6a1f0c2d-1111-4222-8333-944455556666", "7"
    conv = priv("5a" * 32)
    jwk = ecdh_es.jwk_of(conv.public_key())
    meta = {"name": "Ada's scan.png", "type": "image/png", "size": 11}
    body = b"\x89PNG scan!!"
    padded = body + b"\x00" * (4096 - len(body))
    head = ecdh_es.wrap(json.dumps(meta, separators=(",", ":")).encode(), jwk, private_attachments.name_info(c, a, u),
                        _ephemeral=priv("2e" * 32), _nonce=bytes.fromhex("000102030405060708090a0b"))
    sealed_name = private_attachments.NAME_PREFIX + ecdh_es.b64u(
        json.dumps({"c": c, "a": a, "u": u, **head}, separators=(",", ":")).encode())
    wrapped = ecdh_es.wrap(padded, jwk, private_attachments.blob_info(c, a, u),
                           _ephemeral=priv("4d" * 32), _nonce=bytes.fromhex("0c0d0e0f1011121314151617"))
    header = json.dumps({"c": c, "a": a, "u": u, "alg": "ecdh-es", "epk": wrapped["epk"], "n": wrapped["n"]},
                        separators=(",", ":")).encode()
    blob = private_attachments.BLOB_MAGIC + struct.pack(">I", len(header)) + header + ecdh_es.unb64u(wrapped["ct"])
    return {"format": "canopy-chat/v1 files",
            "inputs": {"conversation": c, "attachment": a, "uploader": u,
                       "conversation_priv_scalar_hex": "5a" * 32, "conversation_pub_jwk": jwk,
                       "meta": meta, "bytes_b64u": ecdh_es.b64u(body)},
            "outputs": {"name_info": private_attachments.name_info(c, a, u),
                        "blob_info": private_attachments.blob_info(c, a, u),
                        "sealed_name": sealed_name, "blob_b64u": ecdh_es.b64u(blob)}}


def test_the_file_vectors_are_current():
    """Regenerate with: uv run python -c 'from tests.test_private_attachments import _write; _write()'."""
    assert json.loads(VECTORS.read_text()) == _vector()


def test_canopy_web_accepts_the_vectors_shape_and_never_opens_it():
    v = _vector()
    row = mock.Mock(pk=v["inputs"]["attachment"], uploaded_by_id=v["inputs"]["uploader"])
    conv = v["inputs"]["conversation"]
    assert private_attachments.check_sealed_name(v["outputs"]["sealed_name"], row, conv)
    private_attachments.check_sealed_blob(ecdh_es.unb64u(v["outputs"]["blob_b64u"]), row, conv, max_bytes=10_000)
    src = (VECTORS.parent.parent.parent.parent / "apps/canopy_sessions/private_attachments.py").read_text()
    assert "decrypt" not in src and "AESGCM" not in src


def _write():  # pragma: no cover — a maintainer's regeneration helper
    VECTORS.write_text(json.dumps(_vector(), indent=2) + "\n")


# ── the embedded panel: the person's browser in canopy's frame, not the site's backend ──


def _site_token(w):
    from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken

    app = AppCredential.create_credential(name="connect-labs", created_by=w["user"], workspace=w["ws"])
    AppCredentialAgent.objects.create(app=app, agent=w["agent"])
    raw, _ = DelegatedToken.issue(app=app, user=w["user"], ttl_seconds=600, runner_requirements=["sensitive_data"])
    return raw


def test_the_panels_frame_attaches_a_file_and_the_sites_backend_cannot(w, bucket, django_capture_on_commit_callbacks):
    raw = _site_token(w)
    backend = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    panel = Client(HTTP_AUTHORIZATION=f"Bearer {raw}", HTTP_SEC_FETCH_SITE="same-origin")
    # The site's server, calling canopy with the visitor's token: refused, nothing kept.
    assert _upload(backend, w["hot"]).status_code == 422
    assert not Attachment.objects.filter(session=w["hot"]).exists() and not bucket.objects
    # The panel's frame: the file for the turn, then the sealed copy.
    up = _upload(panel, w["hot"])
    assert up.status_code == 201, up.content
    aid = up.json()["id"]
    sealed_name, blob = _seal(w["hot"], aid, w["user"].pk)
    # Nor can the backend store (or replace) the sealed copy for it.
    assert _post_sealed(backend, aid, sealed_name, blob).status_code == 422
    assert _post_sealed(panel, aid, sealed_name, blob).status_code == 204
    # Sent from the panel, the agent's box receives it once.
    r = panel.post(f"/api/canopy-sessions/{w['hot'].pk}/send", {"text": "what do these say?"},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    turn = Turn.objects.filter(chat_session=w["hot"]).latest("created_at")
    assert turn.origin_ref["attachments"] == [{"id": aid, "sealed": True}]
    box = _box(w)
    _claim(turn, box)
    with django_capture_on_commit_callbacks(execute=True):
        got = _fetch(box, turn, aid)
    assert got.status_code == 200 and got.content == PNG
    # The panel's history lists it and fetches the sealed copy, which its people open.
    [entry] = panel.get(f"/api/canopy-sessions/{w['hot'].pk}/sealed-attachments").json()
    sealed = panel.get(f"/api/canopy-sessions/attachments/{aid}/sealed")
    assert sealed.status_code == 200
    meta, raw_bytes = _open(w["hot"], w["user"], w["key"], entry["sealed_name"], sealed.content)
    assert meta["name"] == NAME and raw_bytes == PNG
