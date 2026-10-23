"""A knowledge base is a version log of whole files its steward built (canopy-web never
builds one, #1971): plan says when nothing changed, whats_new reads the log, rollback
serves an earlier file, and a Drive source's documents are published only when their
owner approved them.

The steward's CLI is played here by hand: it builds, uploads the file (into the fake
bucket) and publishes it with its state.
"""
from __future__ import annotations

import gzip
import hashlib

import pytest
from canopy_kb import build
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import AgentProject, KbDriveFile, KbVersion
from apps.mcp import projects as project_mcp
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures import kb as kb_fixtures

pytestmark = pytest.mark.django_db

# the bucket and project fixtures (a fixture is found by its name in the module)
s3, world = kb_fixtures.s3, kb_fixtures.world

J, KB_URL, ITEMS, V2 = kb_fixtures.J, kb_fixtures.KB_URL, kb_fixtures.ITEMS, kb_fixtures.V2
_bundle, _full, _plan = kb_fixtures.bundle, kb_fixtures.full, kb_fixtures.plan


def _call(user, name, args):
    import apps.mcp.projects as mod

    orig = mod._current_user
    mod._current_user = lambda: user
    try:
        return project_mcp._call("connect", "givewell-kb", name, args)
    finally:
        mod._current_user = orig


def test_an_unchanged_republish_sends_nothing_and_makes_no_version(world, s3, tmp_path):
    b = _bundle(tmp_path, "v1", ITEMS)
    _full(world, s3, tmp_path, b)
    assert _plan(world, b)["unchanged"] and KbVersion.objects.count() == 1


def test_canopy_web_never_builds_an_index(world, s3):
    """#1971: building in the web process ran canopy-web out of memory. The routes that
    took a delta to build are gone, and so is the builder."""
    c = world["client"]
    assert c.post(f"{KB_URL}/versions", {"state": {"docs": {}, "parts": {}}, "base": 1},
                  content_type=J).status_code in (404, 405)
    assert c.post(f"{KB_URL}/blobs", {"blobs": []}, content_type=J).status_code in (404, 405)
    import importlib.util
    assert importlib.util.find_spec("apps.agents.kb_index") is None


def test_a_whole_file_publish_serves_it_keeps_the_last_and_whats_new_reads_the_log(world, s3, tmp_path):
    v1 = _bundle(tmp_path, "v1", ITEMS)
    _full(world, s3, tmp_path, v1)
    first = AgentProject.objects.get(handle="givewell-kb").kb_key
    v2 = _bundle(tmp_path, "v2", V2)
    p = _plan(world, v2)
    assert (p["added"], p["changed"], p["removed"]) == (1, 1, 1) and sorted(p["upload_docs"]) == ["n:1", "p:1"]
    _full(world, s3, tmp_path, v2)
    project = AgentProject.objects.get(handle="givewell-kb")
    assert project.kb_version.number == 2 and project.kb_key != first and first in s3.objects
    out = _call(world["owner"], "whats_new", {})
    assert out["version"] == 2
    assert {(c["item_id"], c["change"]) for c in out["changes"]} >= {("n:1", "new"), ("p:1", "changed"),
                                                                     ("r:1", "removed")}
    assert "Zinc too" in _call(world["owner"], "read", {"ref": "p:1"})


def test_rollback_serves_an_earlier_file(world, s3, tmp_path):
    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    first_key = AgentProject.objects.get(handle="givewell-kb").kb_key
    _full(world, s3, tmp_path, _bundle(tmp_path, "v2", ITEMS, grants={
        "Grant": "ORS", "Recipient": "Dimagi Inc", "Amount": 5, "Date": "2026-01-01"}))
    assert _call(world["owner"], "grants", {"Recipient": "Dimagi"})["rows"][0]["Grant"] == "ORS"
    r = world["client"].post(f"{KB_URL}/versions/1/rollback", content_type=J)
    assert r.status_code == 200, r.content
    assert (r.json()["kind"], r.json()["number"], r.json()["current"]) == ("rollback", 3, True)
    assert AgentProject.objects.get(handle="givewell-kb").kb_key == first_key
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
    drive["project"].kb_readers.add(editor)
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
    """canopy-web vets every whole-file publish of a Drive KB against its owner's
    approvals: a slip in a steward's pipeline cannot put an unapproved file in."""
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
    with pytest.raises(AssertionError, match="Drive documents are not approved"):
        _full(world, s3, tmp_path, _bundle(tmp_path, "d2", [doc, held]))
    assert AgentProject.objects.get(handle="givewell-kb").kb_version.number == 1
    _decide(c, drive, {"file_id": "f-pl", "release_hold": True})
    _full(world, s3, tmp_path, _bundle(tmp_path, "d3", [doc, held]))
    assert AgentProject.objects.get(handle="givewell-kb").kb_version.number == 2


def test_a_drive_kb_publishes_with_its_state(world, drive, s3, tmp_path):
    """Without the state there is nothing to vet: refused."""
    db = tmp_path / "x.sqlite"
    build(_bundle(tmp_path, "x", ITEMS), db)
    body = gzip.compress(db.read_bytes())
    up = world["client"].post(f"{KB_URL}/upload", content_type=J).json()
    s3.objects[up["key"]] = body
    r = world["client"].post(f"{KB_URL}/publish", {"key": up["key"], "sha256": hashlib.sha256(body).hexdigest()},
                             content_type=J)
    assert r.status_code == 422 and "with its state" in r.json()["detail"]


def test_a_drive_kb_answers_only_its_readers(world, drive, s3, tmp_path):
    from apps.mcp.projects import readable_project

    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    member = User.objects.create_user("m", "m@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=member, workspace=world["connect"].parent, role=WorkspaceMembership.EDITOR)
    assert readable_project(member, "connect", "givewell-kb") is None
    assert readable_project(world["owner"], "connect", "givewell-kb") is not None
    world["client"].post(f"{KB_URL}/readers", {"add": ["m@dimagi.com"]}, content_type=J)
    assert readable_project(member, "connect", "givewell-kb") is not None
