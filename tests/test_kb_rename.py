"""A project's knowledge base survives a new address (#2047): everything it stores is
keyed by the project (`kb_store.root`), not by `<workspace>/<handle>`, so a rename moves
nothing and its originals, step results and versions are all still found.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib
import json

import pytest
from django.apps import apps as django_apps

from apps.agents import kb_store, services
from apps.agents.models import AgentProject, ProjectService
from tests.fixtures import kb as kb_fixtures

pytestmark = pytest.mark.django_db

s3, world = kb_fixtures.s3, kb_fixtures.world
ITEMS, V2, _bundle = kb_fixtures.ITEMS, kb_fixtures.V2, kb_fixtures.bundle

J = "application/json"
OLD, NEW = "/api/workspace-projects/connect/givewell-kb/kb", "/api/workspace-projects/connect/givewell/kb"


def _put(s3, upload, body):
    s3.objects[upload["url"].split("https://s3.test/", 1)[1].split("?", 1)[0]] = body


def _result(c, s3, url, obj) -> str:
    raw = json.dumps(obj, sort_keys=True).encode()
    body, key = gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest()
    r = c.post(f"{url}/results", {"blobs": [{"sha256": key, "gz_sha256": hashlib.sha256(body).hexdigest(),
                                             "bytes": len(body)}]}, content_type=J)
    for u in r.json()["uploads"]:
        _put(s3, u, body)
    return key


def _original(c, s3, url, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    r = c.post(f"{url}/originals", {"files": [{"sha256": sha, "bytes": len(data), "content_type": "text/plain"}]},
               content_type=J)
    for u in r.json()["uploads"]:
        _put(s3, u, data)
    return sha


def test_a_renamed_project_keeps_its_originals_results_and_versions(world, s3, tmp_path, monkeypatch):
    c = world["client"]
    one = _result(c, s3, OLD, {"refresh": 1})
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS), curation=one)
    two = _result(c, s3, OLD, {"refresh": 2})
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v2", V2), previous=tmp_path / "v1.sqlite", curation=two)
    sha = _original(c, s3, OLD, b"a transcript the publisher has since removed")

    services.promote_project(AgentProject.objects.get(pk=world["project"].pk), "givewell")
    project = AgentProject.objects.get(pk=world["project"].pk)
    assert project.address == "connect/givewell"

    # a warm refresh: its results and originals are still there, so nothing is re-made or re-sent
    have = c.post(f"{NEW}/objects/have", {"store": "results", "keys": [one, two]}, content_type=J).json()
    assert have == {"have": sorted([one, two])}
    again = c.post(f"{NEW}/originals", {"files": [{"sha256": sha, "bytes": 44}]}, content_type=J).json()
    assert again["have"] == 1 and again["uploads"] == []
    # a reader's `original` still gets its download
    assert kb_store.original_link(project)(sha, "t.txt", "text/plain")
    # and its next publish, and a rollback to a version from before the rename, both work
    monkeypatch.setattr(kb_fixtures, "KB_URL", NEW)
    kb_fixtures.full(world, s3, tmp_path, _bundle(tmp_path, "v3", ITEMS), curation=two)
    r = c.post(f"{NEW}/versions/1/rollback", content_type=J)
    assert r.status_code == 200, r.content
    assert (r.json()["number"], r.json()["curation_sha"]) == (4, one)


def test_a_new_project_stores_under_its_id(world):
    assert kb_store.root(world["project"]) == f"kb-projects/{world['project'].pk}/"


def test_a_project_from_before_keeps_the_root_it_stored_under(world):
    """The migration freezes each existing project's root: the one its served file names
    (a project renamed before the fix stored under its old address), else its address."""
    renamed = world["project"]
    ProjectService.objects.create(project=renamed, kind=ProjectService.KB, name="kb",
                                  kb_key="workspace-projects/connect/old-name/kb/abc.sqlite.gz")
    plain = AgentProject.objects.create(name="Other", workspace=world["connect"], handle="other")
    migration = importlib.import_module("apps.agents.migrations.0090_project_storage_root")

    migration.freeze_roots(django_apps, None)

    assert AgentProject.objects.get(pk=renamed.pk).storage_root == "workspace-projects/connect/old-name/"
    assert AgentProject.objects.get(pk=plain.pk).storage_root == "workspace-projects/connect/other/"
