"""Range requests must cost ONE Drive round-trip, and be cacheable.

Every range the <video> element asked for used to make two sequential Drive
calls: a `download(start=0, end=0)` head probe purely to learn the total, then
the real fetch. Measured against prod that was ~1.93s TTFB vs ~1.37s without
the probe — ~0.55s added to the initial load and to every seek, and a scrub is
several ranges. The total is already on the row (`size_bytes`) and the bytes
behind one walkthrough id never change.
"""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.walkthroughs.models import Walkthrough


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user(
        username="perf@dimagi.com", email="perf@dimagi.com",
    )


def _make(owner, **kw):
    defaults = dict(
        title="Demo", kind="video", owner=owner, visibility="link",
        drive_file_id="file-1", drive_folder_id="folder-1",
        content_type="video/mp4", size_bytes=10,
    )
    defaults.update(kw)
    return Walkthrough.objects.create(**defaults)


@override_settings(REQUIRE_AUTH=True)
def test_a_range_request_makes_exactly_one_drive_call(owner):
    w = _make(owner)
    token = w.ensure_share_token()
    with patch("apps.walkthroughs.streaming.storage.download",
               return_value=(b"2345", 2, 5, 10)) as dl:
        resp = Client().get(f"/walkthrough/{w.id}/content?t={token}", HTTP_RANGE="bytes=2-5")
    assert resp.status_code == 206
    assert dl.call_count == 1, "the size probe is back — every seek costs a second Drive round-trip"
    assert dl.call_args.kwargs["start"] == 2


@override_settings(REQUIRE_AUTH=True)
def test_the_probe_still_happens_when_size_is_unknown(owner):
    """Rows predating size_bytes must keep working."""
    w = _make(owner, size_bytes=0)
    token = w.ensure_share_token()
    with patch("apps.walkthroughs.streaming.storage.download",
               return_value=(b"2345", 2, 5, 10)) as dl:
        resp = Client().get(f"/walkthrough/{w.id}/content?t={token}", HTTP_RANGE="bytes=2-5")
    assert resp.status_code == 206
    assert dl.call_count == 2


@override_settings(REQUIRE_AUTH=True)
def test_the_denominator_comes_from_the_fetch_not_the_row(owner):
    """If size_bytes ever drifted, the published total is still Drive's."""
    w = _make(owner, size_bytes=10)
    token = w.ensure_share_token()
    with patch("apps.walkthroughs.streaming.storage.download",
               return_value=(b"2345", 2, 5, 999)):
        resp = Client().get(f"/walkthrough/{w.id}/content?t={token}", HTTP_RANGE="bytes=2-5")
    assert resp["Content-Range"] == "bytes 2-5/999"


@override_settings(REQUIRE_AUTH=True)
@pytest.mark.parametrize("headers", [{}, {"HTTP_RANGE": "bytes=2-5"}])
def test_the_browser_may_cache_the_bytes_but_no_shared_cache_may(owner, headers):
    w = _make(owner)
    token = w.ensure_share_token()
    with patch("apps.walkthroughs.streaming.storage.download",
               return_value=(b"2345", 2, 5, 10)):
        resp = Client().get(f"/walkthrough/{w.id}/content?t={token}", **headers)
    cc = resp["Cache-Control"]
    assert "private" in cc, "token-gated bytes must never enter a shared cache"
    assert "immutable" in cc and "max-age=" in cc
    assert resp["ETag"] == '"file-1-10"'


@override_settings(REQUIRE_AUTH=True)
def test_an_unsatisfiable_range_still_416s_without_fetching_bytes(owner):
    w = _make(owner, size_bytes=10)
    token = w.ensure_share_token()
    with patch("apps.walkthroughs.streaming.storage.download") as dl:
        resp = Client().get(f"/walkthrough/{w.id}/content?t={token}", HTTP_RANGE="bytes=99-")
    assert resp.status_code == 416
    assert resp["Content-Range"] == "bytes */10"
    assert dl.call_count == 0
