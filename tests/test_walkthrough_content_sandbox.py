"""Uploaded HTML never runs as the viewer.

A walkthrough deck (or a DDD docs page) is HTML an editor or an agent wrote,
served from canopy's own origin. It used to go out as plain `text/html` with no
CSP, framed by viewers sandboxed `allow-scripts allow-same-origin` — which on a
same-origin URL is no sandbox: a script in the deck could call the
cookie-authenticated API as whoever opened it. The content response now carries
a `sandbox` CSP, which holds even when the URL is opened directly (no frame), so
the document always lands in an opaque origin.
"""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.walkthroughs.models import Walkthrough
from apps.workspaces.testing import a_workspace
from apps.walkthroughs.streaming import SANDBOX_CSP


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user(
        username="sbx@dimagi.com", email="sbx@dimagi.com",
    )


def _make(owner, **kw):
    defaults = dict(
        title="Deck", kind="html", owner=owner, visibility="link",
        drive_file_id="file-1", drive_folder_id="folder-1",
        content_type="text/html", size_bytes=10,
    )
    defaults.update(kw)
    defaults.setdefault("workspace", a_workspace())
    return Walkthrough.objects.create(**defaults)


def _get(w, **headers):
    token = w.ensure_share_token()
    with patch("apps.walkthroughs.streaming.storage.download",
               return_value=(b"<html>hi!</html>"[:10], 0, 9, 10)):
        return Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t={token}", **headers)


def test_the_policy_is_an_opaque_origin_sandbox():
    tokens = SANDBOX_CSP.split()
    assert tokens[0] == "sandbox"
    assert "allow-scripts" in tokens, "a deck's own navigation script must still run"
    assert "allow-same-origin" not in tokens, "that would hand the deck canopy's origin back"
    assert "allow-forms" not in tokens
    assert "allow-top-navigation" not in tokens


@override_settings(REQUIRE_AUTH=True)
@pytest.mark.parametrize("headers", [{}, {"HTTP_RANGE": "bytes=0-4"}])
def test_html_content_is_served_sandboxed(owner, headers):
    resp = _get(_make(owner), **headers)
    assert resp.status_code in (200, 206)
    assert resp["Content-Security-Policy"] == SANDBOX_CSP
    assert resp["X-Content-Type-Options"] == "nosniff"
    # Our own viewer still frames it.
    assert resp["X-Frame-Options"] == "SAMEORIGIN"


@override_settings(REQUIRE_AUTH=True)
def test_an_unknown_type_is_sandboxed_too(owner):
    """Fail-safe: anything that is not a video is contained, not trusted."""
    resp = _get(_make(owner, content_type="application/xhtml+xml"))
    assert resp["Content-Security-Policy"] == SANDBOX_CSP


@override_settings(REQUIRE_AUTH=True)
def test_video_is_not_sandboxed_and_range_still_works(owner):
    w = _make(owner, kind="video", content_type="video/mp4")
    token = w.ensure_share_token()
    with patch("apps.walkthroughs.streaming.storage.download",
               return_value=(b"2345", 2, 5, 10)):
        resp = Client().get(f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t={token}", HTTP_RANGE="bytes=2-5")
    assert resp.status_code == 206
    assert resp["Content-Range"] == "bytes 2-5/10"
    assert resp["Content-Type"] == "video/mp4"
    assert "Content-Security-Policy" not in resp
    assert resp["X-Content-Type-Options"] == "nosniff"
