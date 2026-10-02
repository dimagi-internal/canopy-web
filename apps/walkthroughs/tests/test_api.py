"""Contract tests for the walkthroughs Ninja surface (/api/walkthroughs/).

Covers all 6 endpoints:
  POST   /  (multipart upload)
  GET    /  (list + filters)
  GET    /{wid}/
  PATCH  /{wid}/
  DELETE /{wid}/
  POST   /{wid}/rotate-token/

All Drive calls are intercepted by the fake_drive fixture.
"""
from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.walkthroughs import storage
from apps.walkthroughs.models import Walkthrough
from apps.walkthroughs.schemas import WalkthroughDetailOut, WalkthroughListItemOut
from tests.fixtures.fake_drive import FakeDriveClient

from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import ensure_member
from apps.workspaces.testing import a_workspace

User = get_user_model()

BASE = "/api/walkthroughs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_drive(monkeypatch):
    inst = FakeDriveClient()
    monkeypatch.setattr(storage, "get_drive_client", lambda: inst)
    return inst


def _editor(email):
    # Editors of the default workspace: uploading and editing are the author
    # tier, and a walkthrough with no workspace is readable by no member.
    user = User.objects.create_user(username=email, email=email)
    ensure_member(a_workspace(), user, WorkspaceMembership.EDITOR)
    return user


@pytest.fixture
def owner(db):
    return _editor("owner@dimagi.com")


@pytest.fixture
def other_user(db):
    return _editor("other@dimagi.com")


@pytest.fixture
def auth_client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _file_part(name: str, content: bytes, content_type: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type=content_type)


def _make_walkthrough(owner, **kwargs) -> Walkthrough:
    defaults = dict(
        title="test walk",
        kind="html",
        drive_file_id="fake-file",
        drive_folder_id="fake-folder",
        content_type="text/html",
        size_bytes=42,
        workspace=a_workspace(),
    )
    defaults.update(kwargs)
    return Walkthrough.objects.create(owner=owner, **defaults)


# ---------------------------------------------------------------------------
# 1. test_upload_walkthrough_html
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)
def test_upload_walkthrough_html(auth_client, fake_drive, owner):
    html = b"<html><body>hello</body></html>"
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("slideshow.html", html, "text/html"),
            "title": "HTML Demo",
            "kind": "html",
            "visibility": "link",
        },
        format="multipart",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    out = WalkthroughDetailOut.model_validate(body)
    assert out.title == "HTML Demo"
    assert out.kind == "html"
    assert out.is_owner is True
    assert out.visibility == "link"


@pytest.mark.django_db
@override_settings(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)
def test_upload_replaces_same_run_id_and_role(auth_client, fake_drive, owner):
    """Re-uploading the same (run_id, role) supersedes the prior one, so a run
    never holds two of the same role. A different role under the same run is
    untouched."""
    from apps.walkthroughs.models import Walkthrough

    def _upload(role: str, title: str):
        return auth_client.post(
            f"{BASE}/",
            data={
                "file": _file_part("a.html", b"<html>x</html>", "text/html"),
                "title": title,
                "kind": "html",
                "visibility": "link",
                "run_id": "feat-2026-06-01-001",
                "role": role,
            },
            format="multipart",
        )

    # role=deck (slides) — not narrative-gated, so it exercises the replace path.
    first = _upload("deck", "slides v1")
    assert first.status_code == 201
    first_id = first.json()["id"]

    # Same role, same run → replaces.
    second = _upload("deck", "slides v2")
    assert second.status_code == 201
    second_id = second.json()["id"]

    # Different role, same run → coexists.
    clip = _upload("clip", "a clip")
    assert clip.status_code == 201

    rows = Walkthrough.objects.filter(run_id="feat-2026-06-01-001")
    assert not rows.filter(pk=first_id).exists()  # v1 superseded
    assert rows.filter(pk=second_id).exists()  # v2 kept
    assert rows.filter(role="deck").count() == 1
    assert rows.filter(role="clip").count() == 1


# ---------------------------------------------------------------------------
# 1b. test_upload_with_links round-trips the companion links
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)
def test_upload_with_links(auth_client, fake_drive, owner):
    links = [
        {"label": "Back to the narrative", "url": "https://x/review/1/", "kind": "narrative"},
        {"label": "Still-frame walkthrough", "url": "https://x/w/abc", "kind": "companion"},
        {"label": "Sampling designer", "url": "https://labs/microplans/program/133/new/"},
    ]
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("video.mp4", b"\x00\x00", "video/mp4"),
            "title": "Video Demo",
            "kind": "video",
            "links": json.dumps(links),
        },
        format="multipart",
    )
    assert resp.status_code == 201, resp.content
    out = WalkthroughDetailOut.model_validate(resp.json())
    assert [(l.kind, l.label) for l in out.links] == [
        ("narrative", "Back to the narrative"),
        ("companion", "Still-frame walkthrough"),
        ("reference", "Sampling designer"),  # kind defaults to reference
    ]


@pytest.mark.django_db
@override_settings(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)
def test_upload_rejects_malformed_links(auth_client, fake_drive, owner):
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("video.mp4", b"\x00", "video/mp4"),
            "title": "Bad links",
            "kind": "video",
            "links": json.dumps([{"label": "no url"}]),  # missing required url
        },
        format="multipart",
    )
    assert resp.status_code == 422, resp.content


# ---------------------------------------------------------------------------
# 2. test_upload_requires_file
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_upload_requires_file(auth_client):
    resp = auth_client.post(
        f"{BASE}/",
        data={"title": "x", "kind": "html"},
        format="multipart",
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. test_upload_rejects_invalid_kind
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_upload_rejects_invalid_kind(auth_client):
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("a.pdf", b"data", "application/pdf"),
            "title": "x",
            "kind": "pdf",
        },
        format="multipart",
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 4. test_upload_oversize_returns_413
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
    WALKTHROUGH_MAX_UPLOAD_BYTES=10,
)
def test_upload_oversize_returns_413(auth_client, fake_drive):
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("big.html", b"x" * 20, "text/html"),
            "title": "big",
            "kind": "html",
        },
        format="multipart",
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body.get("type", "").endswith("/payload-too-large")


# ---------------------------------------------------------------------------
# 5. test_upload_drive_not_configured_returns_500
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)
def test_upload_drive_not_configured_returns_500(auth_client, monkeypatch):
    from apps.walkthroughs.drive_client import DriveNotConfigured

    def _raise(*args, **kwargs):
        raise DriveNotConfigured("no drive key")

    monkeypatch.setattr(storage, "store_upload", _raise)

    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("s.html", b"<html/>", "text/html"),
            "title": "x",
            "kind": "html",
        },
        format="multipart",
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body.get("type", "").endswith("/drive-not-configured")
    # no orphan row
    assert Walkthrough.objects.count() == 0


# ---------------------------------------------------------------------------
# 6. test_list_walkthroughs
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_list_walkthroughs(auth_client, owner):
    _make_walkthrough(owner, title="alpha")
    _make_walkthrough(owner, title="beta", kind="video", content_type="video/mp4")
    resp = auth_client.get(f"{BASE}/")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    # Validate round-trip through schema
    for item in items:
        WalkthroughListItemOut.model_validate(item)


# ---------------------------------------------------------------------------
# 7. test_list_filter_by_project_kind_mine
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_list_filter_by_project(auth_client, owner, other_user):
    _make_walkthrough(owner, title="canopy-html", project_slug="canopy-web")
    _make_walkthrough(owner, title="ace-html", project_slug="ace-web")
    resp = auth_client.get(f"{BASE}/?project=canopy-web")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["title"] == "canopy-html"


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_list_filter_by_kind(auth_client, owner):
    _make_walkthrough(owner, title="html-walk", kind="html")
    _make_walkthrough(owner, title="vid-walk", kind="video", content_type="video/mp4")
    resp = auth_client.get(f"{BASE}/?kind=video")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["kind"] == "video"


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_list_filter_mine(auth_client, owner, other_user):
    _make_walkthrough(owner, title="mine")
    _make_walkthrough(other_user, title="theirs")
    resp = auth_client.get(f"{BASE}/?mine=true")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["title"] == "mine"


# ---------------------------------------------------------------------------
# 8. test_get_detail_owner_sees_token
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_get_detail_owner_sees_token(auth_client, owner):
    w = _make_walkthrough(owner, visibility="link")
    resp = auth_client.get(f"{BASE}/{w.id}/")
    assert resp.status_code == 200
    body = resp.json()
    out = WalkthroughDetailOut.model_validate(body)
    assert out.is_owner is True


# ---------------------------------------------------------------------------
# 9. test_get_detail_non_owner_no_token
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_get_detail_non_owner_no_token(owner, other_user):
    w = _make_walkthrough(owner, visibility="link")
    c = Client()
    c.force_login(other_user)
    resp = c.get(f"{BASE}/{w.id}/")
    assert resp.status_code == 200
    body = resp.json()
    out = WalkthroughDetailOut.model_validate(body)
    assert out.is_owner is False


# ---------------------------------------------------------------------------
# 10. test_get_404
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_get_404(auth_client):
    bogus = uuid.uuid4()
    resp = auth_client.get(f"{BASE}/{bogus}/")
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("type", "").endswith("/not-found")


# ---------------------------------------------------------------------------
# 11. test_patch_owner_only (non-owner → 403)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_patch_owner_only(owner, other_user):
    w = _make_walkthrough(owner)
    c = Client()
    c.force_login(other_user)
    resp = c.patch(
        f"{BASE}/{w.id}/",
        data=json.dumps({"title": "hacked"}),
        content_type="application/json",
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body.get("type", "").endswith("/forbidden")


# ---------------------------------------------------------------------------
# 12. test_patch_to_link_changes_visibility
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_patch_to_link_changes_visibility(auth_client, owner):
    w = _make_walkthrough(owner, visibility="private")
    resp = auth_client.patch(
        f"{BASE}/{w.id}/",
        data=json.dumps({"visibility": "link"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    out = WalkthroughDetailOut.model_validate(body)
    assert out.visibility == "link"


# ---------------------------------------------------------------------------
# 13. test_delete_owner_returns_204
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
)
def test_delete_owner_returns_204(auth_client, fake_drive, owner):
    # Store a real fake file so delete_stored can find it
    stored = storage.store_upload(
        walkthrough_id=str(uuid.uuid4()),
        filename="slideshow.html",
        content_type="text/html",
        data=b"<html/>",
    )
    w = Walkthrough.objects.create(
        title="t",
        kind="html",
        owner=owner,
        drive_file_id=stored.file_id,
        drive_folder_id=stored.folder_id,
        content_type="text/html",
        size_bytes=7,
        workspace=a_workspace(),
    )
    resp = auth_client.delete(f"{BASE}/{w.id}/")
    assert resp.status_code == 204
    assert not Walkthrough.objects.filter(id=w.id).exists()


# ---------------------------------------------------------------------------
# 14. test_delete_non_owner_403
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=True)
def test_delete_non_owner_403(owner, other_user):
    w = _make_walkthrough(owner)
    c = Client()
    c.force_login(other_user)
    resp = c.delete(f"{BASE}/{w.id}/")
    assert resp.status_code == 403
    body = resp.json()
    assert body.get("type", "").endswith("/forbidden")
    # Row should still exist
    assert Walkthrough.objects.filter(id=w.id).exists()


# ---------------------------------------------------------------------------
# 16. test_endpoints_404_when_disabled
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(WALKTHROUGHS_ENABLED=False)
def test_endpoints_404_when_disabled(auth_client, owner):
    w = _make_walkthrough(owner)

    # POST /
    resp = auth_client.post(
        f"{BASE}/",
        data={"file": _file_part("s.html", b"x", "text/html"), "kind": "html"},
        format="multipart",
    )
    assert resp.status_code == 404

    # GET /
    resp = auth_client.get(f"{BASE}/")
    assert resp.status_code == 404

    # GET /{wid}/
    resp = auth_client.get(f"{BASE}/{w.id}/")
    assert resp.status_code == 404

    # PATCH /{wid}/
    resp = auth_client.patch(
        f"{BASE}/{w.id}/",
        data=json.dumps({"title": "x"}),
        content_type="application/json",
    )
    assert resp.status_code == 404

    # DELETE /{wid}/
    resp = auth_client.delete(f"{BASE}/{w.id}/")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Server-side narrative backstop — refuse terminal DDD artifacts with no
# narrative (mirrors the plugin guard in scripts/ddd/upload.py).
# ---------------------------------------------------------------------------

_DDD_SETTINGS = dict(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)


def _make_narrative_version(narrative_slug, run_id):
    """A story-bearing concept_change review = a narrative version for `narrative_slug`."""
    from apps.reviews.models import ReviewRequest

    return ReviewRequest.objects.create(
        run_id=run_id,
        narrative_slug=narrative_slug,
        version=1,
        gate="concept_change",
        status=ReviewRequest.STATUS_PENDING,
        request_json={"run_id": run_id, "narrative_slug": narrative_slug, "narrative": "A story."},
    )


@pytest.mark.django_db
@override_settings(**_DDD_SETTINGS)
def test_hero_video_without_narrative_is_refused(auth_client, fake_drive, owner):
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("video.mp4", b"\x00\x00", "video/mp4"),
            "title": "hero", "kind": "video",
            "run_id": "verified-monitoring-2026-06-04-001",
            "narrative_slug": "verified-monitoring",
            "role": "hero_video",
        },
        format="multipart",
    )
    assert resp.status_code == 409, resp.content
    assert "no narrative" in resp.json().get("detail", "").lower()
    # Nothing persisted.
    assert not Walkthrough.objects.filter(narrative_slug="verified-monitoring").exists()


@pytest.mark.django_db
@override_settings(**_DDD_SETTINGS)
def test_docs_without_narrative_is_refused(auth_client, fake_drive, owner):
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("slideshow.html", b"<html></html>", "text/html"),
            "title": "deck", "kind": "html",
            "run_id": "verified-monitoring-2026-06-04-001",
            "narrative_slug": "verified-monitoring",
            "role": "docs",
        },
        format="multipart",
    )
    assert resp.status_code == 409, resp.content


@pytest.mark.django_db
@override_settings(**_DDD_SETTINGS)
def test_hero_video_with_narrative_is_allowed(auth_client, fake_drive, owner):
    _make_narrative_version("verified-monitoring", "verified-monitoring-2026-06-04-001")
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("video.mp4", b"\x00\x00", "video/mp4"),
            "title": "hero", "kind": "video",
            "run_id": "verified-monitoring-2026-06-04-001",
            "narrative_slug": "verified-monitoring",
            "role": "hero_video",
        },
        format="multipart",
    )
    assert resp.status_code == 201, resp.content


@pytest.mark.django_db
@override_settings(**_DDD_SETTINGS)
def test_stamped_review_id_bypasses_server_check(auth_client, fake_drive, owner):
    """A supplied narrative_review_id is trusted proof — no narrative row needed."""
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("video.mp4", b"\x00\x00", "video/mp4"),
            "title": "hero", "kind": "video",
            "run_id": "verified-monitoring-2026-06-04-001",
            "narrative_slug": "verified-monitoring",
            "role": "hero_video",
            "narrative_review_id": str(uuid.uuid4()),
        },
        format="multipart",
    )
    assert resp.status_code == 201, resp.content


@pytest.mark.django_db
@override_settings(**_DDD_SETTINGS)
def test_intermediate_deck_clip_not_guarded(auth_client, fake_drive, owner):
    """ddd-run mid-loop uploads (deck/clip) must NOT be blocked by the guard."""
    for role, kind, fname, ct in [
        ("deck", "html", "slideshow.html", "text/html"),
        ("clip", "video", "video.mp4", "video/mp4"),
    ]:
        resp = auth_client.post(
            f"{BASE}/",
            data={
                "file": _file_part(fname, b"\x00\x00", ct),
                "title": role, "kind": kind,
                "run_id": "verified-monitoring-2026-06-04-001",
                "narrative_slug": "verified-monitoring",
                "role": role,
            },
            format="multipart",
        )
        assert resp.status_code == 201, (role, resp.content)


@pytest.mark.django_db
@override_settings(**_DDD_SETTINGS)
def test_non_ddd_walkthrough_share_not_guarded(auth_client, fake_drive, owner):
    """A plain walkthrough-share upload (no role, no narrative_slug) is unaffected."""
    resp = auth_client.post(
        f"{BASE}/",
        data={
            "file": _file_part("slideshow.html", b"<html></html>", "text/html"),
            "title": "share", "kind": "html",
        },
        format="multipart",
    )
    assert resp.status_code == 201, resp.content
