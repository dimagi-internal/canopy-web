"""A refresh runs on any machine: canopy-web keeps what it needs from the last one, step
results by key, immutable and steward-only; the curation state is one of them, named by
the version it built (`tests/test_kb_versions.py`).
"""
from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from canopy_kb import build
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import AgentProject, KbVersion
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures import kb as kb_fixtures

pytestmark = pytest.mark.django_db

s3, world = kb_fixtures.s3, kb_fixtures.world
ITEMS, V2, _bundle = kb_fixtures.ITEMS, kb_fixtures.V2, kb_fixtures.bundle

J = "application/json"
KB_URL = "/api/workspace-projects/connect/givewell-kb/kb"


def _put_result(world, s3, obj) -> str:
    """Upload one step result (here: a curation state) the way the steward does; its key
    is its content's sha256, as for a state."""
    body = gzip.compress(json.dumps(obj, sort_keys=True).encode(), mtime=0)
    key = hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
    r = world["client"].post(f"{KB_URL}/results", {"blobs": [
        {"sha256": key, "gz_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}]}, content_type=J)
    assert r.status_code == 200, r.content
    for u in r.json()["uploads"]:
        s3.objects[u["url"].split("https://s3.test/", 1)[1].split("?", 1)[0]] = body
    return key


def test_results_are_stored_once_by_key_and_fetched_by_link(world, s3):
    c = world["client"]
    key = _put_result(world, s3, {"text": "extracted"})
    again = c.post(f"{KB_URL}/results", {"blobs": [{"sha256": key, "gz_sha256": "1" * 64, "bytes": 3}]},
                   content_type=J).json()
    assert again["have"] == 1 and again["uploads"] == []
    have = c.post(f"{KB_URL}/objects/have", {"store": "results", "keys": [key, "2" * 64]}, content_type=J)
    assert have.json() == {"have": [key]}
    links = c.post(f"{KB_URL}/objects/links", {"store": "originals", "keys": ["3" * 64]}, content_type=J).json()
    assert links["links"][0]["url"].startswith("https://s3.test/workspace-projects/") and "/originals/" in \
        links["links"][0]["url"]
    assert c.post(f"{KB_URL}/objects/have", {"store": "results", "keys": ["nope"]},
                  content_type=J).status_code == 422
    assert c.post(f"{KB_URL}/objects/have", {"store": "elsewhere", "keys": [key]},
                  content_type=J).status_code == 422


def test_only_the_steward_reads_curation(world, s3):
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=world["connect"], role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(viewer)
    assert c.post(f"{KB_URL}/objects/links", {"store": "results", "keys": ["0" * 64]},
                  content_type=J).status_code in (403, 404)


def test_a_whole_file_publish_ends_a_delta_left_from_before(world, s3, tmp_path):
    """A delta canopy-web was to build (before #1971) no longer blocks a publish."""
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    project = AgentProject.objects.get(handle="givewell-kb")
    stuck = KbVersion.objects.create(project=project, number=2, kind=KbVersion.DELTA, status=KbVersion.BUILDING,
                                     base=project.kb_version)
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v3", V2), base=1)
    stuck.refresh_from_db()
    assert stuck.status == KbVersion.FAILED and "superseded" in stuck.error
    assert AgentProject.objects.get(handle="givewell-kb").kb_version.number == 3


def test_a_version_counts_the_content_changes_its_builder_reports(world, s3, tmp_path):
    """The log shows how many documents changed in content, as the builder counted them,
    not every record a new format re-addressed."""
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    db = tmp_path / "v2.sqlite"
    build(_bundle(tmp_path, "v2", V2), db)
    body = gzip.compress(db.read_bytes())
    c = world["client"]
    up = c.post(f"{KB_URL}/upload", content_type=J).json()
    s3.objects[up["key"]] = body
    r = c.post(f"{KB_URL}/publish", {"key": up["key"], "sha256": hashlib.sha256(body).hexdigest(), "base": 1,
                                     "changes": {"added": 1, "changed": 0, "removed": 1}}, content_type=J)
    assert r.status_code == 200, r.content
    v = c.get(f"{KB_URL}/versions").json()[0]
    assert (v["number"], v["added"], v["changed"], v["removed"]) == (2, 1, 0, 1)
