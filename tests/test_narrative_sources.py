"""A narrative's iteration sources (apps/runs/sources.py).

Everything a DDD narrative needs to be iterated again — recipe, master
recording, capture report — must live on canopy-web, pinned to the version it
was made for, so any machine can pick the narrative up. Before this, a recorded
narrative could be published with its recipe and master left in one machine's
temp directory, and re-recording it anywhere else was impossible.
"""
from __future__ import annotations

import hashlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.runs import transfer
from apps.runs.tests.factories import (
    add_member,
    make_review,
    make_user,
    make_workspace,
)
from apps.walkthroughs import storage
from apps.walkthroughs.models import NarrativeSource
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.fake_drive import FakeDriveClient

pytestmark = pytest.mark.django_db

SLUG = "chlorine"
RUN = "chlorine-2026-10-07-002"
_DRIVE = dict(CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder", CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}')
RECIPE = b"style: recorded\ncuts:\n  - id: cut1\n    scenes: [c1-a]\n"


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDriveClient()
    monkeypatch.setattr(storage, "get_drive_client", lambda: fake)
    return fake


@pytest.fixture
def ws(db):
    return make_workspace("connect")


@pytest.fixture
def editor(ws):
    u = make_user("editor@dimagi.com")
    add_member(ws, u)
    return u


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _version(owner, ws, version=1):
    return make_review(
        owner, run_id=RUN, gate="concept_change", narrative_slug=SLUG, version=version,
        workspace=ws,
        request_json={"run_id": RUN, "gate": "concept_change", "narrative": "Story.",
                      "narration": [{"id": "c1-a", "scene": 1, "text": "Register."}]},
    )


def _upload(client, review, role, data, *, slug=SLUG, ws="connect"):
    return client.post(
        f"/api/w/{ws}/ddd/narratives/{slug}/sources/",
        {"file": SimpleUploadedFile("x", data), "role": role,
         "narrative_review_id": str(review.id)},
    )


@override_settings(**_DRIVE)
def test_upload_list_and_download_round_trip(drive, ws, editor):
    v1 = _version(editor, ws)
    c = _client(editor)

    resp = _upload(c, v1, "recipe", RECIPE)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["role"] == "recipe" and body["filename"] == "recipe.yaml"
    assert body["version"] == 1 and body["reused"] is False
    assert body["sha256"] == hashlib.sha256(RECIPE).hexdigest()

    listed = c.get(f"/api/w/connect/ddd/narratives/{SLUG}/sources/").json()
    assert [s["role"] for s in listed] == ["recipe"]

    got = c.get(listed[0]["content_url"])
    assert got.status_code == 200
    assert got.content == RECIPE
    assert got["X-Content-SHA256"] == body["sha256"]


@override_settings(**_DRIVE)
def test_identical_bytes_are_reused_and_a_change_replaces(drive, ws, editor):
    v1 = _version(editor, ws)
    c = _client(editor)
    first = _upload(c, v1, "master", b"mp4-bytes").json()
    again = _upload(c, v1, "master", b"mp4-bytes").json()
    assert again["reused"] is True and again["id"] == first["id"]

    changed = _upload(c, v1, "master", b"mp4-bytes-v2").json()
    assert changed["reused"] is False
    assert list(NarrativeSource.objects.values_list("id", flat=True)) == [
        __import__("uuid").UUID(changed["id"])
    ]


@override_settings(**_DRIVE)
def test_version_payload_lists_its_sources(drive, ws, editor):
    v1 = _version(editor, ws)
    v2 = _version(editor, ws, version=2)
    c = _client(editor)
    _upload(c, v1, "recipe", RECIPE)
    _upload(c, v2, "recipe", RECIPE + b"# v2\n")
    _upload(c, v2, "capture_report", b"{}")

    body = c.get(f"/api/w/connect/ddd/narratives/{SLUG}/").json()
    by_version = {v["version"]: sorted(s["role"] for s in v["sources"]) for v in body["versions"]}
    assert by_version == {2: ["capture_report", "recipe"], 1: ["recipe"]}


@override_settings(**_DRIVE)
def test_a_viewer_cannot_write_and_an_outsider_cannot_see(drive, ws, editor):
    v1 = _version(editor, ws)
    viewer = make_user("viewer@dimagi.com")
    add_member(ws, viewer, role=WorkspaceMembership.VIEWER)
    assert _upload(_client(viewer), v1, "recipe", RECIPE).status_code == 403

    _upload(_client(editor), v1, "recipe", RECIPE)
    src = NarrativeSource.objects.get()
    outsider = make_user("outsider@dimagi.com")
    add_member(make_workspace("elsewhere"), outsider)
    oc = _client(outsider)
    assert oc.get(f"/api/w/elsewhere/ddd/narratives/{SLUG}/sources/").json() == []
    url = f"/api/w/elsewhere/ddd/narratives/{SLUG}/sources/{src.id}/content"
    assert oc.get(url).status_code == 404


@override_settings(**_DRIVE)
def test_a_source_must_pin_to_a_version_of_that_narrative(drive, ws, editor):
    other = make_review(
        editor, run_id="other-2026-10-07-001", gate="concept_change",
        narrative_slug="other", version=1, workspace=ws,
        request_json={"narrative": "Other.", "narration": [{"id": "o", "text": "o"}]},
    )
    resp = _upload(_client(editor), other, "recipe", RECIPE)
    assert resp.status_code == 404


@override_settings(**_DRIVE)
def test_unknown_role_is_refused(drive, ws, editor):
    v1 = _version(editor, ws)
    assert _upload(_client(editor), v1, "cookies", b"secret").status_code == 422


@override_settings(**_DRIVE)
def test_deleting_a_version_or_narrative_drops_its_sources(drive, ws, editor):
    v1 = _version(editor, ws)
    v2 = _version(editor, ws, version=2)
    c = _client(editor)
    _upload(c, v1, "recipe", RECIPE)
    _upload(c, v2, "recipe", RECIPE + b"#2")

    assert c.delete(f"/api/w/connect/ddd/narratives/{SLUG}/versions/1/").status_code == 204
    assert list(NarrativeSource.objects.values_list("narrative_review_id", flat=True)) == [v2.id]

    assert c.delete(f"/api/w/connect/ddd/narratives/{SLUG}/").status_code == 204
    assert not NarrativeSource.objects.exists()


@override_settings(**_DRIVE)
def test_moving_a_narrative_moves_its_sources(drive, ws, editor):
    v1 = _version(editor, ws)
    _upload(_client(editor), v1, "recipe", RECIPE)
    make_workspace("dimagi")
    transfer.apply_move({SLUG}, "dimagi")
    assert NarrativeSource.objects.get().workspace_id == "dimagi"
