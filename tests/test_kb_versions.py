"""A knowledge base is a version log of whole files its steward built (canopy-web never
builds one, #1971). Each version carries the curation state it was built from; a
publish names the version it was made from and loses to one that landed first;
whats_new reads the file's own history; rollback serves an earlier file and state; and
a source is a curator's crawler only (no Drive-folder source).

The steward's refresh is played here by hand: it builds, uploads the file (into the
fake bucket) and publishes it.
"""
from __future__ import annotations

import gzip
import hashlib

import pytest

from apps.agents.models import AgentProject, KbVersion
from apps.mcp import projects as project_mcp
from tests.fixtures import kb as kb_fixtures

pytestmark = pytest.mark.django_db

# the bucket and project fixtures (a fixture is found by its name in the module)
s3, world = kb_fixtures.s3, kb_fixtures.world

J, KB_URL, ITEMS, V2 = kb_fixtures.J, kb_fixtures.KB_URL, kb_fixtures.ITEMS, kb_fixtures.V2
_bundle, _full = kb_fixtures.bundle, kb_fixtures.full


def _call(user, name, args):
    import apps.mcp.projects as mod

    orig = mod._current_user
    mod._current_user = lambda: user
    try:
        return project_mcp._call("connect", "givewell-kb", name, args)
    finally:
        mod._current_user = orig


def _result(world, s3, obj) -> str:
    """Upload a curation state the way a refresh does: as a step result."""
    import json as _json
    body = gzip.compress(_json.dumps(obj).encode())
    key = hashlib.sha256(_json.dumps(obj).encode()).hexdigest()
    ups = world["client"].post(f"{KB_URL}/results", {"blobs": [{"sha256": key, "gz_sha256": hashlib.sha256(body)
                                                                .hexdigest(), "bytes": len(body)}]},
                               content_type=J).json()["uploads"]
    for u in ups:
        s3.objects[u["url"].split("https://s3.test/", 1)[1].split("?", 1)[0]] = body
    return key


def test_canopy_web_never_builds_an_index_nor_keeps_a_refresh_s_state_beside_the_log(world, s3):
    """#1971: building in the web process ran canopy-web out of memory; the routes that
    took a delta to build are gone, and so is the builder. The curation pointer and its
    lease went with them: a version carries its own state."""
    c = world["client"]
    assert c.post(f"{KB_URL}/versions", {"state": {"docs": {}, "parts": {}}, "base": 1},
                  content_type=J).status_code in (404, 405)
    for method, path in (("post", "blobs"), ("post", "plan"), ("get", "curation"), ("put", "curation"),
                         ("post", "curation/lease")):
        assert getattr(c, method)(f"{KB_URL}/{path}", {}, content_type=J).status_code in (404, 405), path
    import importlib.util
    assert importlib.util.find_spec("apps.agents.kb_index") is None


def test_a_version_carries_the_state_it_was_built_from_and_a_rollback_brings_it_back(world, s3, tmp_path):
    one = _result(world, s3, {"refresh": 1})
    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS), curation=one)
    two = _result(world, s3, {"refresh": 2})
    _full(world, s3, tmp_path, _bundle(tmp_path, "v2", V2), curation=two)
    log = world["client"].get(f"{KB_URL}/versions").json()
    assert [(v["number"], v["curation_sha"], v["current"]) for v in log] == [(2, two, True), (1, one, False)]
    r = world["client"].post(f"{KB_URL}/versions/1/rollback", content_type=J).json()
    assert (r["number"], r["curation_sha"], r["current"]) == (3, one, True)  # the next refresh starts from it


def test_a_publish_made_from_an_older_version_loses(world, s3, tmp_path):
    """Two refreshes start from version 1; the second to publish is refused, not applied."""
    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    _full(world, s3, tmp_path, _bundle(tmp_path, "v2", V2), base=1)
    r = _full(world, s3, tmp_path, _bundle(tmp_path, "v2b", ITEMS), base=1, expect=409)
    assert "at version 2, not 1" in r.json()["detail"]
    assert AgentProject.objects.get(handle="givewell-kb").knowledge_base.kb_version.number == 2


def test_a_publish_names_a_stored_curation_state(world, s3, tmp_path):
    r = _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS), curation="0" * 64, expect=422)
    assert "is not stored" in r.json()["detail"] and KbVersion.objects.count() == 0


def test_a_whole_file_publish_serves_it_keeps_the_last_and_whats_new_reads_the_file(world, s3, tmp_path):
    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    first = AgentProject.objects.get(handle="givewell-kb").knowledge_base.kb_key
    _full(world, s3, tmp_path, _bundle(tmp_path, "v2", V2), previous=tmp_path / "v1.sqlite")
    project = AgentProject.objects.get(handle="givewell-kb")
    assert project.knowledge_base.kb_version.number == 2 and project.knowledge_base.kb_key != first and first in s3.objects
    out = _call(world["owner"], "whats_new", {})
    assert {(c["item_id"], c["change"]) for c in out["changes"]} >= {("n:1", "new"), ("p:1", "changed"),
                                                                     ("r:1", "removed")}
    assert ("b:1", "changed") not in {(c["item_id"], c["change"]) for c in out["changes"]}  # unchanged text
    assert "Zinc too" in _call(world["owner"], "read", {"ref": "p:1"})


def test_rollback_serves_an_earlier_file(world, s3, tmp_path):
    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    first_key = AgentProject.objects.get(handle="givewell-kb").knowledge_base.kb_key
    _full(world, s3, tmp_path, _bundle(tmp_path, "v2", ITEMS, grants={
        "Grant": "ORS", "Recipient": "Dimagi Inc", "Amount": 5, "Date": "2026-01-01"}))
    assert _call(world["owner"], "grants", {"Recipient": "Dimagi"})["rows"][0]["Grant"] == "ORS"
    r = world["client"].post(f"{KB_URL}/versions/1/rollback", content_type=J)
    assert r.status_code == 200, r.content
    assert (r.json()["kind"], r.json()["number"], r.json()["current"]) == ("rollback", 3, True)
    assert AgentProject.objects.get(handle="givewell-kb").knowledge_base.kb_key == first_key
    assert _call(world["owner"], "grants", {"Recipient": "Dimagi"})["rows"][0]["Grant"] == "Water pilot"


def test_only_the_newest_files_are_kept(world, s3, tmp_path, monkeypatch):
    from apps.agents import kb_versions

    monkeypatch.setattr(kb_versions, "KEEP_FILES", 2)
    for n in range(4):
        (tmp_path / "kb.sqlite").unlink(missing_ok=True)
        _full(world, s3, tmp_path, _bundle(tmp_path, f"v{n}", [dict(ITEMS[0], text=f"text {n}", sha256=str(n))]))
    kept = list(KbVersion.objects.order_by("number").values_list("number", "kb_key"))
    assert [bool(k) for _, k in kept] == [False, False, True, True]
    assert all(k in s3.objects for _, k in kept if k)
    r = world["client"].post(f"{KB_URL}/versions/1/rollback", content_type=J)
    assert r.status_code == 422 and "pruned" in r.json()["detail"]


# ---- sources ----

def test_a_source_is_a_curators_crawler_and_a_drive_folder_source_is_refused(world):
    """Agents publish curator bundles; there is no Drive-folder source for an owner to
    review file by file."""
    c = world["client"]
    web = c.post(f"{KB_URL}/sources", {"kind": "web", "name": "givewell", "config": {"reads": "givewell.org"}},
                 content_type=J)
    assert web.status_code == 200, web.content
    drive = c.post(f"{KB_URL}/sources", {"kind": "drive_folders", "name": "kmc", "config": {"folders": ["f1"]}},
                   content_type=J)
    assert drive.status_code == 422
    assert [s["kind"] for s in c.get(f"{KB_URL}/sources").json()] == ["web"]
    for gone in ("review", "readers", f"sources/{web.json()['id']}/files"):
        assert c.get(f"{KB_URL}/{gone}").status_code in (404, 405)
