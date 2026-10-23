"""A refresh runs on any machine: canopy-web keeps what it needs from the last one (step
results and the curation state), and builds a knowledge base's first version from its
records, so no machine ever builds kb.sqlite.
"""
from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from canopy_kb import build, records
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


def test_a_curation_state_is_saved_by_compare_and_swap(world, s3):
    """Two refreshes start from one state; the second to save is refused, never applied."""
    c = world["client"]
    assert c.get(f"{KB_URL}/curation").json()["sha"] is None
    s1 = _put_result(world, s3, {"refresh": 1})
    assert c.put(f"{KB_URL}/curation", {"sha": s1, "base": None}, content_type=J).json()["sha"] == s1
    s2, raced = _put_result(world, s3, {"refresh": 2}), _put_result(world, s3, {"refresh": "raced"})
    assert c.put(f"{KB_URL}/curation", {"sha": s2, "base": s1}, content_type=J).status_code == 200
    r = c.put(f"{KB_URL}/curation", {"sha": raced, "base": s1}, content_type=J)
    assert r.status_code == 409 and b"another refresh" in r.content
    assert c.get(f"{KB_URL}/curation").json()["sha"] == s2


def test_a_curation_state_must_be_uploaded_first(world, s3):
    r = world["client"].put(f"{KB_URL}/curation", {"sha": "0" * 64}, content_type=J)
    assert r.status_code == 422 and b"no curation state" in r.content


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
    assert c.get(f"{KB_URL}/curation").status_code in (403, 404)
    assert c.post(f"{KB_URL}/objects/links", {"store": "results", "keys": ["0" * 64]},
                  content_type=J).status_code in (403, 404)


def test_one_refresh_at_a_time(world, s3):
    c = world["client"]
    r = c.post(f"{KB_URL}/curation/lease", {"holder": "run-a", "seconds": 600}, content_type=J)
    assert r.status_code == 200 and r.json()["lease"]["holder"] == "run-a"
    other = c.post(f"{KB_URL}/curation/lease", {"holder": "run-b"}, content_type=J)
    assert other.status_code == 409 and b"run-a" in other.content
    assert c.post(f"{KB_URL}/curation/lease", {"holder": "run-a"}, content_type=J).status_code == 200  # renew
    key = _put_result(world, s3, {"refresh": "a"})
    saved = c.put(f"{KB_URL}/curation", {"sha": key}, content_type=J).json()
    assert saved["sha"] == key and saved["lease"]["holder"] == "run-a"  # saving keeps the lease
    assert c.delete(f"{KB_URL}/curation/lease?holder=run-a").status_code == 200
    assert c.post(f"{KB_URL}/curation/lease", {"holder": "run-b"}, content_type=J).status_code == 200
    assert c.get(f"{KB_URL}/curation").json()["sha"] == key


def test_a_whole_file_publish_ends_a_delta_left_from_before(world, s3, tmp_path):
    """A delta canopy-web was to build (before #1971) no longer blocks a publish."""
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    project = AgentProject.objects.get(handle="givewell-kb")
    stuck = KbVersion.objects.create(project=project, number=2, kind=KbVersion.DELTA, status=KbVersion.BUILDING,
                                     base=project.kb_version)
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v3", V2))
    stuck.refresh_from_db()
    assert stuck.status == KbVersion.FAILED and "superseded" in stuck.error
    assert AgentProject.objects.get(handle="givewell-kb").kb_version.number == 3


def test_a_whole_file_publish_logs_the_changes_its_builder_reports(world, s3, tmp_path):
    """A refresh's runner says which documents changed in content; `whats_new` lists those,
    not every record a new format re-addressed."""
    v1 = _bundle(tmp_path, "v1", ITEMS)
    kb_fixtures.full(world, s3, tmp_path, v1)
    db = tmp_path / "v2.sqlite"
    v2 = _bundle(tmp_path, "v2", V2)
    build(v2, db)
    body = gzip.compress(db.read_bytes())
    c = world["client"]
    up = c.post(f"{KB_URL}/upload", content_type=J).json()
    s3.objects[up["key"]] = body
    r = c.post(f"{KB_URL}/publish", {"key": up["key"], "sha256": hashlib.sha256(body).hexdigest(),
                                     "state": records.state(v2),
                                     "changes": {"added": ["n:1"], "changed": [], "removed": ["r:1"],
                                                 "rows": {"n:1": {"type": "blog_post", "title": "New grant"}}}},
               content_type=J)
    assert r.status_code == 200, r.content
    from apps.agents.models import KbChange
    logged = {(ch.item_id, ch.change, ch.title) for ch in KbChange.objects.filter(version__number=2)}
    assert logged == {("n:1", "new", "New grant"), ("r:1", "removed", "")}
