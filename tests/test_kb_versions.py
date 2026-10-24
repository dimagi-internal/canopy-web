"""A knowledge base is a version log of whole files its steward built (canopy-web never
builds one, #1971). Each version carries the curation state it was built from; a
publish names the version it was made from and loses to one that landed first;
whats_new reads the file's own history; rollback serves an earlier file and state; and
a Drive source's file is opened and published only when its owner approved every
document in it.

The steward's refresh is played here by hand: it builds, uploads the file (into the
fake bucket) and publishes it.
"""
from __future__ import annotations

import gzip
import hashlib

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import kb_store
from apps.agents.models import AgentProject, KbDriveFile, KbVersion
from apps.mcp import projects as project_mcp
from apps.workspaces.models import WorkspaceMembership
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


# ---- Drive sources ----

ROOT = "root-folder-1"


def _listing(**over):
    """A synthetic folder: protocols, a raw-data subfolder, a patient list, one file
    shared more narrowly than its folder."""
    team = ["user:hal@dimagi-ai.com", "user:jj@dimagi.com"]
    files = [
        {"id": ROOT, "name": "Kangaroo care (synthetic)", "mime_type": "application/vnd.google-apps.folder",
         "readers": team},
        {"id": "f-proto", "parent": ROOT, "name": "KMC protocol", "mime_type": "application/vnd.google-apps.document",
         "version": "7", "owner": "hal@dimagi-ai.com", "readers": team},
        {"id": "d-raw", "parent": ROOT, "name": "Raw data", "mime_type": "application/vnd.google-apps.folder",
         "readers": team},
        {"id": "f-pl", "parent": "d-raw", "name": "Patient list.xlsx", "md5": "m1", "owner": "hal@dimagi-ai.com",
         "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "readers": team},
        {"id": "f-narrow", "parent": ROOT, "name": "Budget.pdf", "mime_type": "application/pdf", "md5": "m2",
         "readers": ["user:hal@dimagi-ai.com"]},
    ]
    for f in over.pop("add", []):
        files.append(f)
    drop = set(over.pop("drop", []))
    return [f for f in files if f["id"] not in drop]


@pytest.fixture()
def drive(world, s3):
    project = AgentProject.objects.get(handle="givewell-kb")
    project.owner_user = world["owner"]
    project.save()
    r = world["client"].post(f"{KB_URL}/sources", {"kind": "drive_folders", "name": "kmc",
                                                   "config": {"folders": [ROOT]}}, content_type=J)
    assert r.status_code == 200, r.content
    return {"project": project, "source": r.json()["id"]}


def _inventory(world, drive, files, client=None):
    r = (client or world["client"]).post(f"{KB_URL}/sources/{drive['source']}/inventory",
                                         {"files": files, "page_token": "tok"}, content_type=J)
    assert r.status_code == 200, r.content
    return r.json()


def _decide(client, drive, *decisions):
    return client.post(f"{KB_URL}/review/approvals", {"source": drive["source"], "files": list(decisions)},
                       content_type=J)


def test_the_inventory_reads_metadata_flags_names_and_skips_narrow_sharing(world, drive):
    counts = _inventory(world, drive, _listing())
    assert (counts["files"], counts["folders"], counts["skipped"]) == (3, 2, 1)
    review = world["client"].get(f"{KB_URL}/review").json()
    files = {f["file_id"]: f for f in review["sources"][0]["files"]}
    assert files["f-pl"]["flags"] and any("Patient list" in f for f in files["f-pl"]["flags"])
    assert any("Raw data" in f for f in files["f-pl"]["flags"]) and files["f-pl"]["path"] == "Kangaroo care (synthetic) / Raw data"
    assert files["f-narrow"]["skip_reason"].startswith("shared more narrowly")
    assert all(f["decision"] == "pending" for f in files.values()) and review["may_decide"] is True
    # nothing is readable before the owner approves
    assert world["client"].get(f"{KB_URL}/sources/{drive['source']}/files?readable=true").json() == []


def test_only_the_owner_approves_and_a_folder_reaches_what_it_holds(world, drive):
    _inventory(world, drive, _listing())
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=world["connect"].parent, role=WorkspaceMembership.EDITOR)
    ec = Client()
    ec.force_login(editor)
    assert ec.get(f"{KB_URL}/review").status_code == 404  # a Drive KB: not every member sees its files
    kb_store.knowledge_base(drive["project"], create=True).kb_readers.add(editor)
    assert ec.get(f"{KB_URL}/review").json()["may_decide"] is False
    assert _decide(ec, drive, {"file_id": ROOT, "decision": "approved"}).status_code == 403

    assert _decide(world["client"], drive, {"file_id": "f-pl", "decision": "excluded"}).status_code == 200
    assert _decide(world["client"], drive, {"file_id": ROOT, "decision": "approved"}).status_code == 200
    readable = world["client"].get(f"{KB_URL}/sources/{drive['source']}/files?readable=true").json()
    assert sorted(f["file_id"] for f in readable) == ["f-proto"]  # excluded stays excluded; narrow is skipped


def test_new_files_queue_unless_the_folder_auto_approves_and_deleted_ones_are_gone(world, drive):
    _inventory(world, drive, _listing())
    _decide(world["client"], drive, {"file_id": ROOT, "decision": "approved"})
    new_doc = {"id": "f-new", "parent": ROOT, "name": "Follow-up schedule", "readers": ["user:hal@dimagi-ai.com",
               "user:jj@dimagi.com"], "mime_type": "application/vnd.google-apps.document", "version": "1"}
    counts = _inventory(world, drive, _listing(add=[new_doc], drop=["f-proto"]))
    assert (counts["new"], counts["queued"], counts["gone"]) == (1, 1, 1)
    files = {f["file_id"]: f for f in world["client"].get(f"{KB_URL}/review").json()["sources"][0]["files"]}
    assert files["f-new"]["queued"] and files["f-new"]["decision"] == "pending"
    assert files["f-proto"]["present"] is False and not files["f-proto"]["indexable"]

    _decide(world["client"], drive, {"file_id": ROOT, "auto_approve": True})
    later = dict(new_doc, id="f-later", name="Discharge checklist")
    counts = _inventory(world, drive, _listing(add=[new_doc, later], drop=["f-proto"]))
    assert counts["auto_approved"] == 1
    flagged = dict(new_doc, id="f-consent", name="Consent forms scan")
    _inventory(world, drive, _listing(add=[new_doc, later, flagged], drop=["f-proto"]))
    assert KbDriveFile.objects.get(file_id="f-consent").decision == "pending"  # a flag is never auto-approved


def test_a_held_spreadsheet_and_an_unapproved_file_cannot_be_published(world, drive, s3, tmp_path):
    """canopy-web opens every file published to a Drive KB and vets each document in it
    against its owner's approvals: a slip in a steward's pipeline cannot put an
    unapproved file in, whatever the publish says."""
    _inventory(world, drive, _listing())
    _decide(world["client"], drive, {"file_id": ROOT, "decision": "approved"})
    c = world["client"]
    hold = c.post(f"{KB_URL}/sources/{drive['source']}/files/f-pl/hold", {"headers": ["Mother name", "DOB"]},
                  content_type=J)
    assert hold.status_code == 200 and hold.json()["indexable"] is False
    doc = {"id": "drive:f-proto", "type": "gdoc", "title": "KMC protocol", "url": "https://docs/x",
           "text": "Skin to skin.", "sha256": "p", "meta": {"source": "drive", "drive": {"file_id": "f-proto"}}}
    _full(world, s3, tmp_path, _bundle(tmp_path, "d1", [doc]))
    held = {"id": "drive:f-pl", "type": "sheet", "title": "Patient list", "url": "https://docs/y", "text": "x",
            "sha256": "q", "meta": {"source": "drive"}}
    r = _full(world, s3, tmp_path, _bundle(tmp_path, "d2", [doc, held]), expect=422)
    assert "1 Drive documents are not approved" in r.json()["detail"] and "drive:f-pl" in r.json()["detail"]
    assert AgentProject.objects.get(handle="givewell-kb").knowledge_base.kb_version.number == 1
    _decide(c, drive, {"file_id": "f-pl", "release_hold": True})
    _full(world, s3, tmp_path, _bundle(tmp_path, "d3", [doc, held]))
    assert AgentProject.objects.get(handle="givewell-kb").knowledge_base.kb_version.number == 2


def test_a_drive_kb_publish_that_is_not_a_knowledge_base_is_refused(world, drive, s3, tmp_path):
    """Nothing to vet is nothing to serve."""
    body = gzip.compress(b"not a database at all" * 100)
    up = world["client"].post(f"{KB_URL}/upload", content_type=J).json()
    s3.objects[up["key"]] = body
    r = world["client"].post(f"{KB_URL}/publish", {"key": up["key"], "sha256": hashlib.sha256(body).hexdigest(),
                                                   "base": None}, content_type=J)
    assert r.status_code == 422 and "not a knowledge base" in r.json()["detail"]


def test_a_drive_kb_answers_only_its_readers(world, drive, s3, tmp_path):
    from apps.mcp.projects import readable_project

    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    member = User.objects.create_user("m", "m@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=member, workspace=world["connect"].parent, role=WorkspaceMembership.EDITOR)
    assert readable_project(member, "connect", "givewell-kb") is None
    assert readable_project(world["owner"], "connect", "givewell-kb") is not None
    world["client"].post(f"{KB_URL}/readers", {"add": ["m@dimagi.com"]}, content_type=J)
    assert readable_project(member, "connect", "givewell-kb") is not None
