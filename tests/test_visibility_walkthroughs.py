"""Token-gated visibility behaviour for the walkthrough content stream + detail."""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.walkthroughs import storage
from apps.walkthroughs.models import Walkthrough
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import ensure_member
from apps.workspaces.testing import a_workspace
from tests.fixtures.fake_drive import FakeDriveClient


@pytest.fixture
def owner(db):
    # An editor of the walkthroughs' workspace: a walkthrough with no workspace
    # has no members (only its token reads it), and editing one is the author tier.
    user = get_user_model().objects.create_user(
        username="owner@dimagi.com", email="owner@dimagi.com",
    )
    ensure_member(a_workspace(), user, WorkspaceMembership.EDITOR)
    return user


def _member(email, role=WorkspaceMembership.VIEWER):
    user = get_user_model().objects.create_user(username=email, email=email)
    ensure_member(a_workspace(), user, role)
    return user


@pytest.fixture
def fake_drive(monkeypatch):
    """Stub the Drive client so an upload test stays offline (mirrors the
    fixture in tests/test_walkthroughs_drive.py)."""
    inst = FakeDriveClient()
    monkeypatch.setattr(storage, "get_drive_client", lambda: inst)
    return inst


def _make(owner, **kw):
    defaults = dict(
        title="Demo", kind="video", owner=owner,
        drive_file_id="file-1", drive_folder_id="folder-1",
        content_type="video/mp4", size_bytes=10, workspace=a_workspace(),
    )
    defaults.update(kw)
    return Walkthrough.objects.create(**defaults)


# Streaming returns bytes; stub the Drive download so tests stay offline.
# storage.download returns (data, start, end_inclusive, total).
def _stub_download():
    return patch(
        "apps.walkthroughs.streaming.storage.download",
        return_value=(b"data", 0, 3, 4),
    )


@override_settings(REQUIRE_AUTH=True)
def test_public_content_404s_anonymous_without_token(owner):
    w = _make(owner, visibility="link")
    w.ensure_share_token()
    resp = Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_public_content_served_to_anonymous_with_token(owner):
    w = _make(owner, visibility="link")
    token = w.ensure_share_token()
    with _stub_download():
        resp = Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t={token}")
    assert resp.status_code == 200


@override_settings(REQUIRE_AUTH=True)
def test_public_content_404s_anonymous_with_wrong_token(owner):
    w = _make(owner, visibility="link")
    w.ensure_share_token()
    resp = Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t=nope")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_private_content_404s_anonymous_even_with_token(owner):
    w = _make(owner, visibility="private")
    token = w.ensure_share_token()
    resp = Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t={token}")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_private_content_404s_anonymous(owner):
    w = _make(owner, visibility="private")
    resp = Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_malformed_content_id_is_a_bare_404_not_the_spa(owner):
    # The content route accepts <str:wid>, so a non-UUID id is handled by the
    # view (bare 404) instead of falling through to the SPA catch-all and
    # painting the whole app inside a failed embed (issue #345). Authed, so this
    # proves the id itself 404s — not the anonymous gate.
    client = Client()
    client.force_login(owner)
    resp = client.get(f"/w/{a_workspace().slug}/walkthrough/not-a-uuid/content")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_owner_sees_private_content(owner):
    w = _make(owner, visibility="private")
    client = Client()
    client.force_login(owner)
    with _stub_download():
        resp = client.get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content")
    assert resp.status_code == 200


@override_settings(REQUIRE_AUTH=True)
def test_authed_non_owner_sees_private_content(owner):
    # Any MEMBER of the walkthrough's workspace reads it, a viewer included —
    # not just the uploader.
    w = _make(owner, visibility="private")
    other = _member("other@dimagi.com")
    client = Client()
    client.force_login(other)
    with _stub_download():
        resp = client.get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content")
    assert resp.status_code == 200


def test_detail_handler_404s_private_for_anonymous(owner):
    """With auth=None the handler itself must hide private rows from anonymous."""
    from django.test import RequestFactory
    from django.contrib.auth.models import AnonymousUser
    from apps.walkthroughs.api import get_walkthrough

    w = _make(owner, visibility="private")
    req = RequestFactory().get(f"/api/walkthroughs/{w.id}/")
    req.user = AnonymousUser()
    with pytest.raises(Exception):  # Http404 / ProblemError → not found
        get_walkthrough(req, w.id)


@override_settings(REQUIRE_AUTH=True)
def test_public_detail_api_reachable_anonymous(owner):
    w = _make(owner, visibility="link")
    token = w.ensure_share_token()
    resp = Client().get(f"/api/walkthroughs/{w.id}/?t={token}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(w.id)


@override_settings(REQUIRE_AUTH=True)
def test_private_detail_api_404s_anonymous(owner):
    w = _make(owner, visibility="private")
    resp = Client().get(f"/api/walkthroughs/{w.id}/")
    # Reaches the handler (allowlisted) and the handler hides it.
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_detail_api_404s_anonymous_without_token(owner):
    w = _make(owner, visibility="link")
    w.ensure_share_token()
    resp = Client().get(f"/api/walkthroughs/{w.id}/")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_detail_api_serves_anonymous_with_token(owner):
    w = _make(owner, visibility="link")
    token = w.ensure_share_token()
    resp = Client().get(f"/api/walkthroughs/{w.id}/?t={token}")
    assert resp.status_code == 200
    assert resp.json()["is_owner"] is False


@override_settings(REQUIRE_AUTH=True)
def test_detail_api_404s_anonymous_with_wrong_token(owner):
    w = _make(owner, visibility="link")
    w.ensure_share_token()
    resp = Client().get(f"/api/walkthroughs/{w.id}/?t=nope")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_detail_api_404s_private_even_with_token(owner):
    w = _make(owner, visibility="private")
    token = w.ensure_share_token()
    resp = Client().get(f"/api/walkthroughs/{w.id}/?t={token}")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_walkthrough_shell_served_to_anonymous(owner):
    w = _make(owner, visibility="link")
    resp = Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}")
    # Middleware passes the request through (not a login redirect).
    # spa_view returns 200 when the frontend is built, 503 when it isn't
    # (test environment has no build output). Either way, auth did not block it.
    assert resp.status_code != 302, "Anonymous user was redirected to login"
    assert resp.status_code in (200, 503)


@override_settings(REQUIRE_AUTH=True)
def test_walkthrough_collection_still_gated(db):
    # The list/upload collection must NOT be public.
    resp = Client().get("/api/walkthroughs/")
    assert resp.status_code == 401


@override_settings(REQUIRE_AUTH=True)
def test_content_is_served_only_under_its_own_workspace(owner):
    # One address per walkthrough (canopy-web#1337): the bytes under another
    # workspace 404 exactly like a missing id, and nothing redirects.
    w = _make(owner, visibility="link")
    token = w.ensure_share_token()
    a_workspace("elsewhere")
    with _stub_download():
        assert Client().get(f"/w/elsewhere/walkthrough/{w.id}/content?t={token}").status_code == 404
        assert Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t={token}").status_code == 200


@override_settings(REQUIRE_AUTH=True)
def test_legacy_content_paths_are_gone_not_forwarded(owner):
    w = _make(owner, visibility="link")
    for path in (f"/w/{w.id}/content", f"/walkthrough/{w.id}/content"):
        resp = Client().get(path)
        assert resp.status_code == 404, path
        assert "Location" not in resp


@override_settings(REQUIRE_AUTH=True)
def test_patch_to_public_mints_token_and_returns_share_url(owner):
    w = _make(owner, visibility="private")
    assert w.share_token is None
    client = Client()
    client.force_login(owner)
    resp = client.patch(
        f"/api/walkthroughs/{w.id}/",
        data={"visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    w.refresh_from_db()
    assert w.share_token
    assert body["share_url"] is not None
    assert body["share_url"].endswith(f"/w/{w.workspace_id}/walkthrough/{w.id}?t={w.share_token}")


@override_settings(REQUIRE_AUTH=True)
def test_patch_to_private_keeps_token_and_hides_share_url(owner):
    w = _make(owner, visibility="link")
    token = w.ensure_share_token()
    client = Client()
    client.force_login(owner)
    resp = client.patch(
        f"/api/walkthroughs/{w.id}/",
        data={"visibility": "private"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    w.refresh_from_db()
    assert w.share_token == token  # kept — rotation is explicit
    assert resp.json()["share_url"] is None


@override_settings(REQUIRE_AUTH=True)
def test_share_url_hidden_from_non_owner_and_anonymous(owner):
    w = _make(owner, visibility="link")
    token = w.ensure_share_token()

    # Anonymous with a valid token: readable, but share_url is None and the
    # raw token is not a response field.
    resp = Client().get(f"/api/walkthroughs/{w.id}/?t={token}")
    assert resp.status_code == 200
    assert resp.json()["share_url"] is None
    assert "share_token" not in resp.json()

    # Authed member who did not upload it (and does not own the workspace): same.
    other = _member("other2@dimagi.com", WorkspaceMembership.EDITOR)
    client = Client()
    client.force_login(other)
    resp = client.get(f"/api/walkthroughs/{w.id}/")
    assert resp.status_code == 200
    assert resp.json()["share_url"] is None


@override_settings(REQUIRE_AUTH=True, CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder")
def test_upload_with_link_visibility_mints_share_token(owner, fake_drive):
    client = Client()
    client.force_login(owner)
    upload = SimpleUploadedFile(
        "slideshow.html", b"<html>hi</html>", content_type="text/html",
    )
    resp = client.post(
        "/api/walkthroughs/",
        data={
            "file": upload,
            "title": "Demo",
            "kind": "html",
            "visibility": "link",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["share_url"] is not None
    assert "?t=" in body["share_url"]
    w = Walkthrough.objects.get(pk=body["id"])
    assert w.share_token


@override_settings(REQUIRE_AUTH=True)
def test_rotate_invalidates_old_token_and_returns_new_share_url(owner):
    w = _make(owner, visibility="link")
    old = w.ensure_share_token()
    client = Client()
    client.force_login(owner)
    resp = client.post(f"/api/walkthroughs/{w.id}/rotate-token")
    assert resp.status_code == 200
    w.refresh_from_db()
    assert w.share_token != old
    assert f"?t={w.share_token}" in resp.json()["share_url"]
    # Old token is dead on both surfaces.
    assert Client().get(f"/api/walkthroughs/{w.id}/?t={old}").status_code == 404
    assert Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t={old}").status_code == 404
    # New token works.
    assert Client().get(f"/api/walkthroughs/{w.id}/?t={w.share_token}").status_code == 200


@override_settings(REQUIRE_AUTH=True)
def test_anonymous_mutations_still_blocked(owner):
    w = _make(owner, visibility="link")
    w.ensure_share_token()
    c = Client()
    patch_resp = c.patch(
        f"/api/walkthroughs/{w.id}/",
        data={"visibility": "private"},
        content_type="application/json",
    )
    assert patch_resp.status_code == 401
    delete_resp = c.delete(f"/api/walkthroughs/{w.id}/")
    assert delete_resp.status_code == 401
    # Anonymous POST to a non-rotate subpath must not pass the middleware either
    # (no such route exists, but the middleware must reject before routing).
    post_resp = c.post(f"/api/walkthroughs/{w.id}/")
    assert post_resp.status_code == 401
    w.refresh_from_db()
    assert w.visibility == "link"


@override_settings(REQUIRE_AUTH=True)
def test_rotate_is_owner_only(owner):
    w = _make(owner, visibility="link")
    w.ensure_share_token()
    # Anonymous → 401 (middleware/session-auth rejection; rotate is no longer
    # a public-with-manual-owner-check route).
    assert Client().post(f"/api/walkthroughs/{w.id}/rotate-token").status_code == 401
    # Authed NON-MEMBER → 404 (hidden, matching the tokens-app pattern). A
    # member who may read it but not re-key it gets 403 — see
    # tests/test_product_acl.py.
    other = get_user_model().objects.create_user(
        username="other3@dimagi.com", email="other3@dimagi.com",
    )
    client = Client()
    client.force_login(other)
    assert client.post(f"/api/walkthroughs/{w.id}/rotate-token").status_code == 404
