"""A knowledge base publishes its differences: a version log, an incremental index
that equals a full build, rollback, and Drive sources nobody reads until their owner
approves them.

The steward's CLI is played here by hand: it computes a state, plans, PUTs the
records the plan asks for (into the fake bucket), and commits; the index job
(`kb_index.drain`) then builds the version.
"""
from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import kb_index, kb_store
from apps.agents.models import AgentProject, KbDriveFile, KbVersion
from apps.mcp import projects as project_mcp
from apps.workspaces.models import WorkspaceMembership
from canopy_kb import KB, build, records
from tests.fixtures import kb as kb_fixtures

pytestmark = pytest.mark.django_db

# the bucket and project fixtures (a fixture is found by its name in the module)
s3, world = kb_fixtures.s3, kb_fixtures.world

J = "application/json"
KB_URL = "/api/workspace-projects/connect/givewell-kb/kb"

ITEMS = [
    {"id": "p:1", "type": "podcast_episode", "title": "Diarrheal disease", "url": "https://gw/1", "date": "2026-07-23",
     "text": "WASHmobile follows up households after a cholera case.", "sha256": "a",
     "card": {"summary": "A podcast on cholera.", "orgs": ["Dimagi"]}},
    {"id": "b:1", "type": "blog_post", "title": "Safe water", "url": "https://gw/2", "date": "2025-10-29",
     "text": "Chlorination pilots in six countries.", "sha256": "b", "meta": {"linked_from": ["https://gw/1"]}},
    {"id": "r:1", "type": "research", "title": "ORS and zinc", "url": "https://gw/3", "date": "2024",
     "text": "# ORS\n\nOral rehydration salts and zinc cut diarrhoea deaths.", "sha256": "c"},
]


def _bundle(tmp_path, name, items, grants=None):
    b = tmp_path / name
    (b / "tables").mkdir(parents=True)
    (b / "manifest.json").write_text(json.dumps({
        "name": "givewell", "publisher": "GiveWell",
        "tables": {"grants": {"description": "Every grant.", "date_column": "Date", "amount_column": "Amount",
                              "filter_columns": ["Recipient"]}}}))
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    (b / "tables" / "grants.jsonl").write_text(json.dumps(grants or {
        "Grant": "Water pilot", "Recipient": "Dimagi Inc", "Amount": 1069324, "Date": "2025-12-03"}))
    return b


def _full(world, s3, tmp_path, bundle):
    """The first publish: the whole file, with its state."""
    db = tmp_path / f"{bundle.name}.sqlite"
    build(bundle, db)
    body = gzip.compress(db.read_bytes())
    c = world["client"]
    up = c.post(f"{KB_URL}/upload", content_type=J).json()
    s3.objects[up["key"]] = body
    r = c.post(f"{KB_URL}/publish", {"key": up["key"], "sha256": hashlib.sha256(body).hexdigest(),
                                     "state": records.state(bundle)}, content_type=J)
    assert r.status_code == 200, r.content
    return r


def _delta(world, s3, bundle, client=None):
    """Plan, upload what the plan asks for, commit; returns (plan, commit response, bytes sent)."""
    c = client or world["client"]
    state = records.state(bundle)
    plan = c.post(f"{KB_URL}/plan", {"state": state}, content_type=J)
    assert plan.status_code == 200, plan.content
    p = plan.json()
    if p["unchanged"]:
        return p, None, 0
    recs = {iid: rec for iid, _, rec in records.records(bundle)}
    parts = records.parts(bundle)
    texts = dict(records.texts(bundle, set(p["upload_docs"])))
    objs = [recs[i] for i in p["upload_docs"]] + list(texts.values()) + [parts[n] for n in p["upload_parts"]]
    blobs = [{"sha256": records.sha(o), "gz_sha256": hashlib.sha256(records.encode(o)).hexdigest(),
              "bytes": len(records.encode(o))} for o in objs]
    sent = 0
    if blobs:
        ups = c.post(f"{KB_URL}/blobs", {"blobs": blobs}, content_type=J)
        assert ups.status_code == 200, ups.content
        by = {records.sha(o): o for o in objs}
        for u in ups.json()["uploads"]:
            key = u["url"].split("https://s3.test/", 1)[1].split("?", 1)[0]
            s3.objects[key] = records.encode(by[u["sha256"]])
            sent += len(s3.objects[key])
    r = c.post(f"{KB_URL}/versions", {"state": state, "base": p["base"], "uploaded_bytes": sent}, content_type=J)
    return p, r, sent


def _call(user, name, args):
    import apps.mcp.projects as mod

    orig = mod._current_user
    mod._current_user = lambda: user
    try:
        return project_mcp._call("connect", "givewell-kb", name, args)
    finally:
        mod._current_user = orig


V2 = [dict(ITEMS[0], text=ITEMS[0]["text"] + " Zinc too.", sha256="a2"),  # changed
      ITEMS[1],                                                           # same
      {"id": "n:1", "type": "blog_post", "title": "New grant", "url": "https://gw/9", "date": "2026-10-20",
       "text": "A new grant for in-line chlorination.", "sha256": "n"}]   # r:1 removed, n:1 new


def test_an_unchanged_republish_sends_nothing_and_makes_no_version(world, s3, tmp_path):
    b = _bundle(tmp_path, "v1", ITEMS)
    _full(world, s3, tmp_path, b)
    before = len(s3.objects)
    plan, commit, sent = _delta(world, s3, b)
    assert plan["unchanged"] and commit is None and sent == 0
    assert len(s3.objects) == before
    r = world["client"].post(f"{KB_URL}/versions", {"state": records.state(b), "base": 1}, content_type=J)
    assert r.status_code == 200 and r.json()["created"] is False
    assert KbVersion.objects.count() == 1


def test_a_delta_uploads_only_what_changed_and_the_index_matches_a_full_build(world, s3, tmp_path):
    v1 = _bundle(tmp_path, "v1", ITEMS)
    _full(world, s3, tmp_path, v1)
    v2 = _bundle(tmp_path, "v2", V2)
    plan, commit, sent = _delta(world, s3, v2)
    assert (plan["added"], plan["changed"], plan["removed"]) == (1, 1, 1)
    assert sorted(plan["upload_docs"]) == ["n:1", "p:1"] and plan["upload_parts"] == []
    assert commit.status_code == 200, commit.content
    assert commit.json()["version"]["status"] == "pending" and commit.json()["version"]["uploaded_bytes"] == sent
    project = AgentProject.objects.get(handle="givewell-kb")
    served_before = project.kb_key

    kb_index.drain()

    v = KbVersion.objects.get(number=2)
    assert v.status == "ready", v.error
    project.refresh_from_db()
    assert project.kb_version_id == v.pk and project.kb_key == v.kb_key != served_before
    assert served_before in s3.objects  # v1's file is kept for rollback
    # the server's index is the full build of the same content
    full = tmp_path / "full.sqlite"
    build(v2, full)
    served = KB(kb_store.local_path(project))
    want = KB(full)
    assert {k: served.summary[k] for k in ("items", "passages", "entities", "links", "tables")} == \
        {k: want.summary[k] for k in ("items", "passages", "entities", "links", "tables")}
    for q in ("cholera", "chlorination grant", "zinc"):
        assert served.find(q) == want.find(q)
    # whats_new reads the version log
    out = _call(world["owner"], "whats_new", {})
    assert out["version"] == 2
    assert {(c["item_id"], c["change"]) for c in out["changes"]} == {("n:1", "new"), ("p:1", "changed"),
                                                                    ("r:1", "removed")}
    assert next(c for c in out["changes"] if c["item_id"] == "r:1")["title"] == "ORS and zinc"
    assert "Zinc too" in _call(world["owner"], "read", {"ref": "p:1"})


def test_a_changed_table_ships_as_a_part_and_rollback_serves_the_old_file(world, s3, tmp_path):
    v1 = _bundle(tmp_path, "v1", ITEMS)
    _full(world, s3, tmp_path, v1)
    first_key = AgentProject.objects.get(handle="givewell-kb").kb_key
    v2 = _bundle(tmp_path, "v2", ITEMS, grants={"Grant": "ORS", "Recipient": "Dimagi Inc", "Amount": 5,
                                                 "Date": "2026-01-01"})
    plan, commit, _ = _delta(world, s3, v2)
    assert plan["upload_docs"] == [] and plan["upload_parts"] == ["table:grants"]
    kb_index.drain()
    assert _call(world["owner"], "grants", {"Recipient": "Dimagi"})["rows"][0]["Grant"] == "ORS"

    r = world["client"].post(f"{KB_URL}/versions/1/rollback", content_type=J)
    assert r.status_code == 200, r.content
    assert (r.json()["kind"], r.json()["number"], r.json()["current"]) == ("rollback", 3, True)
    project = AgentProject.objects.get(handle="givewell-kb")
    assert project.kb_key == first_key
    assert _call(world["owner"], "grants", {"Recipient": "Dimagi"})["rows"][0]["Grant"] == "Water pilot"
    # the next delta builds on the rolled-back state
    plan, commit, _ = _delta(world, s3, v2)
    assert plan["base"] == 3 and plan["upload_parts"] == ["table:grants"]
    log = world["client"].get(f"{KB_URL}/versions").json()
    assert [(v["number"], v["kind"], v["status"]) for v in log] == [
        (4, "delta", "pending"), (3, "rollback", "ready"), (2, "delta", "ready"), (1, "full", "ready")]


def test_a_stale_base_is_a_conflict_and_a_missing_record_is_refused(world, s3, tmp_path):
    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    v2 = _bundle(tmp_path, "v2", V2)
    c = world["client"]
    stale = c.post(f"{KB_URL}/versions", {"state": records.state(v2), "base": 7}, content_type=J)
    assert stale.status_code == 409
    missing = c.post(f"{KB_URL}/versions", {"state": records.state(v2), "base": 1}, content_type=J)
    assert missing.status_code == 422 and "not uploaded" in missing.json()["detail"]


def test_a_build_that_fails_leaves_the_published_version_serving(world, s3, tmp_path):
    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    project = AgentProject.objects.get(handle="givewell-kb")
    key = project.kb_key
    v2 = _bundle(tmp_path, "v2", V2)
    _delta(world, s3, v2)
    # corrupt one uploaded record: its content no longer matches its address
    rec_key = next(k for k in s3.objects if "/records/" in k and k.endswith(records.state(v2)["docs"]["n:1"]))
    s3.objects[rec_key] = records.encode({"id": "n:1", "text": "tampered"})
    kb_index.drain()
    v = KbVersion.objects.get(number=2)
    assert v.status == "failed" and "is not" in v.error
    project.refresh_from_db()
    assert project.kb_key == key and project.kb_version.number == 1
    assert "WASHmobile" in _call(world["owner"], "read", {"ref": "p:1"})


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


def test_a_held_spreadsheet_and_an_unapproved_file_cannot_be_indexed(world, drive, s3, tmp_path):
    _inventory(world, drive, _listing())
    _decide(world["client"], drive, {"file_id": ROOT, "decision": "approved"})
    c = world["client"]
    hold = c.post(f"{KB_URL}/sources/{drive['source']}/files/f-pl/hold", {"headers": ["Mother name", "DOB"]},
                  content_type=J)
    assert hold.status_code == 200 and hold.json()["indexable"] is False
    doc = {"id": "drive:f-proto", "type": "gdoc", "title": "KMC protocol", "url": "https://docs/x",
           "text": "Skin to skin.", "sha256": "p", "meta": {"source": "drive", "drive": {"file_id": "f-proto"}}}
    v1 = _bundle(tmp_path, "d1", [doc])
    _full(world, s3, tmp_path, v1)
    held = {"id": "drive:f-pl", "type": "sheet", "title": "Patient list", "url": "https://docs/y", "text": "x",
            "sha256": "q", "meta": {"source": "drive"}}
    plan, commit, _ = _delta(world, s3, _bundle(tmp_path, "d2", [doc, held]))
    assert commit.status_code == 422 and "not approved for indexing" in commit.json()["detail"]
    _decide(c, drive, {"file_id": "f-pl", "release_hold": True})
    plan, commit, _ = _delta(world, s3, _bundle(tmp_path, "d3", [doc, held]))
    assert commit.status_code == 200, commit.content


def test_a_drive_kb_answers_only_its_readers(world, drive, s3, tmp_path):
    from apps.mcp.projects import readable_project

    _full(world, s3, tmp_path, _bundle(tmp_path, "v1", ITEMS))
    member = User.objects.create_user("m", "m@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=member, workspace=world["connect"].parent, role=WorkspaceMembership.EDITOR)
    assert readable_project(member, "connect", "givewell-kb") is None
    assert readable_project(world["owner"], "connect", "givewell-kb") is not None
    world["client"].post(f"{KB_URL}/readers", {"add": ["m@dimagi.com"]}, content_type=J)
    assert readable_project(member, "connect", "givewell-kb") is not None
