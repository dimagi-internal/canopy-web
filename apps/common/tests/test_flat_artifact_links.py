"""Flat artifact links are gone (canopy-web#1337): one URL per artifact, under
its workspace, and nothing forwarded. A flat or pre-tenancy `/w/<uuid>` link
answers a plain 404 that says links now include the workspace."""
from __future__ import annotations

import pytest
from django.test import Client

from apps.common.middleware import _is_flat_artifact, _is_scoped_viewer

UUID = "2f9a1c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"

FLAT = [
    f"/walkthrough/{UUID}",
    f"/walkthrough/{UUID}/content",
    f"/review/{UUID}/",
    "/share/sometoken",
    f"/w/{UUID}",
    f"/w/{UUID}/content",
]


@pytest.mark.parametrize("path", FLAT)
def test_a_flat_link_is_a_plain_404_not_a_redirect(path, settings):
    settings.REQUIRE_AUTH = True
    resp = Client().get(path + "?t=tok")
    assert resp.status_code == 404
    assert "include the workspace" in resp.content.decode()
    assert "Location" not in resp


@pytest.mark.parametrize("path", FLAT)
def test_the_flat_matcher_names_each_retired_address(path):
    assert _is_flat_artifact(path)


@pytest.mark.parametrize("path", [
    "/w/dimagi", "/w/dimagi/agents", "/w/dimagi/walkthroughs", "/walkthroughs",
    "/reviews", "/sessions", "/shareouts",
])
def test_the_flat_matcher_spares_live_routes(path):
    assert not _is_flat_artifact(path)


def test_a_workspace_slug_is_never_mistaken_for_a_legacy_uuid():
    assert _is_scoped_viewer(f"/w/connect/walkthrough/{UUID}/content")
    assert not _is_flat_artifact(f"/w/connect/walkthrough/{UUID}/content")
