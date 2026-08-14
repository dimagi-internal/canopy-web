"""Contract tests for the /api/sessions + /api/share surfaces."""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.session_sharing.models import Session

User = get_user_model()


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="owner@dimagi.com", email="owner@dimagi.com")


@pytest.fixture
def other(db):
    return User.objects.create_user(username="other@dimagi.com", email="other@dimagi.com")


@pytest.fixture
def auth_client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _transcript(session_id: str = "sess-1", *, secret: bool = False) -> bytes:
    text = "the key is sk-abcdEFGH1234567890ijklMNOP" if secret else "hello"
    rows = [
        {"type": "system", "subtype": "init", "session_id": session_id},
        {"type": "user", "message": {"content": text}},
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "text", "text": "hi"}]},
        },
    ]
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


def _upload(client, content: bytes, **fields):
    data = {
        "file": SimpleUploadedFile("session.jsonl", content, content_type="application/x-ndjson"),
        "visibility": "link",
        **fields,
    }
    return client.post("/api/sessions/upload", data=data, format="multipart")


@pytest.mark.django_db
def test_upload_mints_share_token_and_counts_redactions(auth_client):
    resp = _upload(auth_client, _transcript(secret=True), title="My Session")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["message_count"] == 2
    assert body["visibility"] == "link"
    assert body["share_token"]
    assert body["redaction_count"] == 1
    assert body["duplicate"] is False


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_public_share_view_served_to_anonymous(auth_client):
    token = _upload(auth_client, _transcript(secret=True)).json()["share_token"]
    resp = Client().get(f"/api/share/{token}")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["redaction_count"] == 1
    assert len(body["messages"]) == 2
    # Secret never reaches the public payload.
    assert "sk-abcd" not in json.dumps(body)
    # No owner identity leaked.
    assert "owner_email" not in body


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_revoked_token_404s(auth_client):
    slug = _upload(auth_client, _transcript()).json()["slug"]
    token = Session.objects.get(slug=slug).active_token().token
    # Make private → revokes the active token.
    auth_client.patch(
        f"/api/sessions/{slug}",
        data=json.dumps({"visibility": "private"}),
        content_type="application/json",
    )
    assert Client().get(f"/api/share/{token}").status_code == 404


@pytest.mark.django_db
def test_rotate_token_invalidates_old_link(auth_client):
    up = _upload(auth_client, _transcript()).json()
    slug, old = up["slug"], up["share_token"]
    new = auth_client.post(f"/api/sessions/{slug}/rotate-token").json()["share_token"]
    assert new != old
    assert Client().get(f"/api/share/{old}").status_code == 404
    assert Client().get(f"/api/share/{new}").status_code == 200


@pytest.mark.django_db
def test_reupload_is_idempotent(auth_client):
    first = _upload(auth_client, _transcript("dup-1")).json()
    second = _upload(auth_client, _transcript("dup-1")).json()
    assert second["duplicate"] is True
    assert second["slug"] == first["slug"]
    assert Session.objects.filter(cli_session_id="dup-1").count() == 1


@pytest.mark.django_db
def test_upload_reports_the_owning_account(auth_client):
    """A PRIVATE upload has no share token and no public page, so the only true
    thing a client can say about it is who can read it — and only the server
    knows that. Agents upload under their OWN accounts, so the uploader is
    frequently not the human who asked for the share; a client that guesses
    prints a false promise (dimagi-internal/canopy#484)."""
    private = _upload(auth_client, _transcript("own-1"), visibility="private").json()
    assert private["share_token"] is None
    assert private["owner_email"] == "owner@dimagi.com"

    linked = _upload(auth_client, _transcript("own-2")).json()
    assert linked["owner_email"] == "owner@dimagi.com"

    # The idempotent re-upload path returns a second, separately-built payload.
    again = _upload(auth_client, _transcript("own-1"), visibility="private").json()
    assert again["duplicate"] is True
    assert again["owner_email"] == "owner@dimagi.com"


@pytest.mark.django_db
def test_arc_create_reports_the_owning_account(auth_client):
    slug = _upload(auth_client, _transcript("arc-own")).json()["slug"]
    body = _create_arc(
        auth_client, [{"session_slug": slug, "heading": "One"}], visibility="private"
    ).json()
    assert body["share_token"] is None
    assert body["owner_email"] == "owner@dimagi.com"


@pytest.mark.django_db
def test_list_includes_link_shared_sessions_from_others(auth_client, other):
    """A session an agent (or teammate) shared must be findable by everyone —
    the list is "shared with the team", not "uploaded by me". Their PRIVATE
    sessions stay invisible."""
    _upload(auth_client, _transcript("mine"))
    other_client = Client()
    other_client.force_login(other)
    _upload(other_client, _transcript("theirs-link"), title="Agent share")
    _upload(other_client, _transcript("theirs-private"), visibility="private")

    rows = auth_client.get("/api/sessions/").json()
    by_title = {r["title"]: r for r in rows}
    assert len(rows) == 2
    theirs = by_title["Agent share"]
    assert theirs["is_owner"] is False
    assert theirs["owner_email"] == "other@dimagi.com"
    # A non-owner still needs the link to open it.
    assert theirs["share_token"]
    assert Client().get(f"/api/share/{theirs['share_token']}").status_code == 200


@pytest.mark.django_db
def test_list_never_exposes_others_private_sessions(auth_client, other):
    other_client = Client()
    other_client.force_login(other)
    _upload(other_client, _transcript("theirs"), visibility="private")
    assert auth_client.get("/api/sessions/").json() == []


@pytest.mark.django_db
def test_non_owner_cannot_read_detail(auth_client, other):
    slug = _upload(auth_client, _transcript()).json()["slug"]
    other_client = Client()
    other_client.force_login(other)
    assert other_client.get(f"/api/sessions/{slug}").status_code == 403


# ---------------------------------------------------------------------------
# Arcs
# ---------------------------------------------------------------------------


def _create_arc(client, items, *, title="My Arc", visibility="link"):
    return client.post(
        "/api/sessions/arcs",
        data=json.dumps({"title": title, "visibility": visibility, "items": items}),
        content_type="application/json",
    )


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_arc_create_and_public_view_stitches_sections_in_order(auth_client):
    s1 = _upload(auth_client, _transcript("arc-a"), title="First").json()["slug"]
    s2 = _upload(auth_client, _transcript("arc-b"), title="Second").json()["slug"]
    resp = _create_arc(
        auth_client,
        [
            {"session_slug": s2, "heading": "Kickoff"},  # deliberately out of upload order
            {"session_slug": s1, "heading": "Follow-up"},
        ],
        title="Campaign build",
    )
    assert resp.status_code == 201, resp.content
    token = resp.json()["share_token"]
    assert resp.json()["item_count"] == 2

    body = Client().get(f"/api/share/{token}").json()
    assert body["kind"] == "arc"
    assert body["title"] == "Campaign build"
    assert [s["heading"] for s in body["sections"]] == ["Kickoff", "Follow-up"]
    # Each section carries its session's reduced messages.
    assert all(len(s["messages"]) == 2 for s in body["sections"])
    assert "owner_email" not in body


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_single_session_token_still_returns_kind_session(auth_client):
    token = _upload(auth_client, _transcript()).json()["share_token"]
    body = Client().get(f"/api/share/{token}").json()
    assert body["kind"] == "session"
    assert len(body["messages"]) == 2
    assert body["sections"] == []


@pytest.mark.django_db
def test_arc_create_rejects_unowned_session(auth_client, other):
    other_client = Client()
    other_client.force_login(other)
    theirs = _upload(other_client, _transcript("theirs")).json()["slug"]
    resp = _create_arc(auth_client, [{"session_slug": theirs}])
    assert resp.status_code == 404


@pytest.mark.django_db
def test_arc_create_rejects_duplicate_session(auth_client):
    s1 = _upload(auth_client, _transcript("dup")).json()["slug"]
    resp = _create_arc(auth_client, [{"session_slug": s1}, {"session_slug": s1}])
    assert resp.status_code == 422


@pytest.mark.django_db
def test_arc_rotate_token_invalidates_old_link(auth_client):
    s1 = _upload(auth_client, _transcript("r1")).json()["slug"]
    arc = _create_arc(auth_client, [{"session_slug": s1}]).json()
    slug, old = arc["slug"], arc["share_token"]
    new = auth_client.post(f"/api/sessions/arcs/{slug}/rotate-token").json()["share_token"]
    assert new != old
    assert Client().get(f"/api/share/{old}").status_code == 404
    assert Client().get(f"/api/share/{new}").status_code == 200


@pytest.mark.django_db
def test_arc_list_and_detail_owner_only(auth_client, other):
    s1 = _upload(auth_client, _transcript("d1"), title="One").json()["slug"]
    slug = _create_arc(auth_client, [{"session_slug": s1, "heading": "H"}]).json()["slug"]

    rows = auth_client.get("/api/sessions/arcs").json()
    assert len(rows) == 1 and rows[0]["slug"] == slug and rows[0]["item_count"] == 1

    detail = auth_client.get(f"/api/sessions/arcs/{slug}").json()
    assert detail["items"][0]["session_slug"] == s1
    assert detail["items"][0]["heading"] == "H"
    assert detail["items"][0]["message_count"] == 2

    other_client = Client()
    other_client.force_login(other)
    assert other_client.get(f"/api/sessions/arcs/{slug}").status_code == 403


@pytest.mark.django_db
def test_arc_list_includes_link_shared_arcs_from_others(auth_client, other):
    s1 = _upload(auth_client, _transcript("v1")).json()["slug"]
    _create_arc(auth_client, [{"session_slug": s1}], title="Their arc")

    other_client = Client()
    other_client.force_login(other)
    rows = other_client.get("/api/sessions/arcs").json()
    assert len(rows) == 1
    assert rows[0]["title"] == "Their arc"
    assert rows[0]["is_owner"] is False
    assert rows[0]["share_token"]


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_arc_make_private_revokes_share(auth_client):
    s1 = _upload(auth_client, _transcript("p1")).json()["slug"]
    arc = _create_arc(auth_client, [{"session_slug": s1}]).json()
    slug, token = arc["slug"], arc["share_token"]
    assert Client().get(f"/api/share/{token}").status_code == 200
    auth_client.patch(
        f"/api/sessions/arcs/{slug}",
        data=json.dumps({"visibility": "private"}),
        content_type="application/json",
    )
    assert Client().get(f"/api/share/{token}").status_code == 404


# ---------------------------------------------------------------------------
# Session timing (when / how long) on the share payload
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_single_session_view_carries_timing_and_turn_count(auth_client):
    token = _upload(
        auth_client, _transcript("tm1"),
        started_at="2026-06-18T23:00:00Z", ended_at="2026-06-19T01:30:00Z",
    ).json()["share_token"]
    body = Client().get(f"/api/share/{token}").json()
    assert body["kind"] == "session"
    assert body["turn_count"] == 1  # one human prompt in _transcript
    assert body["started_at"].startswith("2026-06-18")
    assert body["ended_at"].startswith("2026-06-19")


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_arc_sections_carry_timing_and_arc_spans_them(auth_client):
    s1 = _upload(
        auth_client, _transcript("ta"), title="First",
        started_at="2026-06-18T23:00:00Z", ended_at="2026-06-19T01:00:00Z",
    ).json()["slug"]
    s2 = _upload(
        auth_client, _transcript("tb"), title="Second",
        started_at="2026-06-20T08:00:00Z", ended_at="2026-06-20T10:00:00Z",
    ).json()["slug"]
    token = _create_arc(
        auth_client,
        [{"session_slug": s1, "heading": "H1"}, {"session_slug": s2, "heading": "H2"}],
    ).json()["share_token"]

    body = Client().get(f"/api/share/{token}").json()
    sec = body["sections"]
    assert sec[0]["started_at"].startswith("2026-06-18")
    assert sec[0]["turn_count"] == 1
    assert sec[1]["ended_at"].startswith("2026-06-20")
    # Arc span = earliest start, latest end, summed turns.
    assert body["started_at"].startswith("2026-06-18")
    assert body["ended_at"].startswith("2026-06-20")
    assert body["turn_count"] == 2


@pytest.mark.django_db
def test_reupload_backfills_timing(auth_client):
    # First upload without timing, then re-upload (dedup) WITH timing → backfilled.
    slug = _upload(auth_client, _transcript("bf1")).json()["slug"]
    assert Session.objects.get(slug=slug).started_at is None
    _upload(
        auth_client, _transcript("bf1"),
        started_at="2026-06-18T23:00:00Z", ended_at="2026-06-19T01:00:00Z",
        active_seconds="900",
    )
    s = Session.objects.get(slug=slug)
    assert s.started_at is not None and s.ended_at is not None
    assert s.active_seconds == 900


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_active_seconds_in_share_payload_and_arc_sum(auth_client):
    s1 = _upload(auth_client, _transcript("as1"), active_seconds="600").json()["slug"]
    s2 = _upload(auth_client, _transcript("as2"), active_seconds="1800").json()["slug"]
    tok1 = Session.objects.get(slug=s1).active_token().token
    assert Client().get(f"/api/share/{tok1}").json()["active_seconds"] == 600
    token = _create_arc(
        auth_client, [{"session_slug": s1}, {"session_slug": s2}]
    ).json()["share_token"]
    body = Client().get(f"/api/share/{token}").json()
    assert [sec["active_seconds"] for sec in body["sections"]] == [600, 1800]
    assert body["active_seconds"] == 2400
