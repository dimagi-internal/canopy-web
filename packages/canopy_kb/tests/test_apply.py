"""A delta applied to the previous index is the full build of the same content."""
import json
import sqlite3

import canopy_kb.kb
import pytest
from canopy_kb import KB, apply, build, records
from test_canopy_kb import REVIEW, _bundle


@pytest.fixture(autouse=True)
def _one_clock(monkeypatch):
    """Every build here stamps `first_seen` with the wall clock to the second, and these tests
    compare two builds row for row — so two builds a second boundary apart differ. Pin it."""
    monkeypatch.setattr(canopy_kb.kb, "_now", lambda: "2026-10-23T00:00:00Z")

V2_ITEMS = [
    # p:1 changed text and card; b:1 unchanged; r:amf removed; r:amf2 card-only change; two new
    {"id": "p:1", "type": "podcast_episode", "title": "Diarrheal disease", "url": "https://x/1",
     "date": "2026-07-23", "text": "Cholera spreads through contaminated water. ORS and zinc save lives.",
     "sha256": "a2", "card": {"summary": "A podcast on cholera, ORS and zinc.", "orgs": ["Dimagi"],
                              "programs": ["ORS and zinc"]}},
    {"id": "b:1", "type": "blog_post", "title": "Safe water", "url": "https://x/2",
     "date": "2025-10-29", "text": "Chlorination pilots in six countries.", "sha256": "b",
     "meta": {"linked_from": ["https://x/1"]},
     "card": {"summary": "Chlorination pilots.", "orgs": ["Dimagi"], "programs": ["in-line chlorination"]}},
    {"id": "r:amf2", "type": "charity_review", "title": "Against Malaria Foundation – December 2023 version",
     "url": "https://x/amf-2023", "date": "2023-12", "text": "Newer review.", "sha256": "d",
     "card": {"summary": "The 2023 review of nets.", "orgs": ["Against Malaria Foundation (AMF)"]}},
    {"id": "r:ors", "type": "research", "title": "ORS and zinc", "url": "https://x/ors", "date": "2026-09",
     "text": REVIEW.replace("Against Malaria Foundation", "ORS and zinc"), "sha256": "e",
     "meta": {"linked_from": ["p:1"]}, "card": {"summary": "Intervention report.", "programs": ["oral rehydration"]}},
    {"id": "pdf:1", "type": "pdf_document", "title": "Report", "url": "https://x/r.pdf", "date": "2026",
     "text": "Nets in use: 74%.", "sha256": "f",
     "original": {"sha256": "1" * 64, "filename": "r.pdf", "content_type": "application/pdf", "bytes": 10,
                  "source_url": "https://x/r.pdf", "fetched_at": "2026-10-01T00:00:00Z", "path": "/local/r"}},
]
V2_ALIASES = {"program": [{"canonical": "ORS and zinc", "names": ["ORS and zinc", "oral rehydration"]},
                          {"canonical": "Water chlorination", "names": ["water chlorination", "in-line chlorination"]}]}
QUERIES = ["cholera water", "ORS zinc", "nets", "chlorination pilots", '"cost-effectiveness"', "review"]


def _dump(path):
    db = sqlite3.connect(path)
    out = {}
    for name, sql in {
        "items": "SELECT id, type, title, url, date, sha256, fetched_at, first_seen, meta, chars FROM items ORDER BY id",
        "chunks": 'SELECT item_id, ord, start, "end", title, heading, context, body FROM chunks ORDER BY item_id, ord',
        "sections": "SELECT * FROM sections ORDER BY item_id, ord",
        "cards": "SELECT * FROM cards ORDER BY item_id",
        "cards_fts": "SELECT * FROM cards_fts ORDER BY item_id",
        "links": "SELECT * FROM links ORDER BY src, dst",
        "entities": "SELECT * FROM entities ORDER BY id",
        "item_entities": "SELECT * FROM item_entities ORDER BY item_id, entity_id",
        "table_rows": "SELECT * FROM table_rows ORDER BY tbl, n",
        "grants": "SELECT * FROM grants ORDER BY 1",
        "originals": "SELECT item_id, sha256, filename, bytes, superseded_at != '' FROM originals ORDER BY 1, 2",
    }.items():
        out[name] = db.execute(sql).fetchall()
    out["fts"] = {q: db.execute("SELECT c.item_id, c.ord, round(bm25(chunks_fts), 6) FROM chunks_fts f JOIN chunks c "
                                "ON c.id = f.rowid WHERE chunks_fts MATCH ? ORDER BY 3, 1, 2", (q,)).fetchall()
                  for q in QUERIES}
    summary = json.loads(db.execute("SELECT value FROM meta WHERE key = 'summary'").fetchone()[0])
    out["summary"] = {k: v for k, v in summary.items() if k not in ("built_at", "new", "changed", "removed", "baseline")}
    db.close()
    return out


def _v2(tmp_path):
    b = _bundle(tmp_path / "v2", items=V2_ITEMS, aliases=V2_ALIASES)
    (b / "tables" / "grants.jsonl").write_text(json.dumps(
        {"Grant": "ORS scale-up", "Recipient": "Dimagi Inc", "Amount": 5, "Date": "2026-01-01", "url": "https://g/3"}))
    return b


def _delta(old_bundle, new_bundle):
    d = records.diff(records.state(old_bundle), records.state(new_bundle))
    texts = dict(records.texts(new_bundle))
    recs = {iid: records.join(rec, texts[rec["text_sha"]]) for iid, _, rec in records.records(new_bundle)}
    parts = records.parts(new_bundle)
    tables = records.tables({n: records.decode(records.encode(parts[n])) for n in d["parts"]})  # as stored
    return d, [recs[i] for i in d["added"] + d["changed"]], parts, tables


def test_applying_a_delta_matches_a_full_build_of_the_same_content(tmp_path):
    v1 = _bundle(tmp_path / "v1")
    a = tmp_path / "a.sqlite"
    build(v1, a)
    v2 = _v2(tmp_path)
    full = tmp_path / "full.sqlite"
    build(v2, full, previous=a)
    d, upserts, parts, tables = _delta(v1, v2)
    assert d["removed"] == ["r:amf"] and set(d["added"]) == {"r:ors", "pdf:1"} and "b:1" not in d["changed"]
    inc = tmp_path / "inc.sqlite"
    s = apply(a, inc, upserts, d["removed"], manifest=parts["manifest"], aliases=parts["aliases"], tables=tables)
    assert s["applied"] == {"new": ["pdf:1", "r:ors"], "changed": ["p:1", "r:amf2"], "content_changed": ["p:1"],
                            "removed": ["r:amf"]}
    assert _dump(inc) == _dump(full)
    k_inc, k_full = KB(inc), KB(full)
    for q in QUERIES:
        assert k_inc.find(q) == k_full.find(q)
    assert k_inc.entity("ORS and zinc") == k_full.entity("ORS and zinc")
    assert k_inc.overview() == k_full.overview()
    assert {(c["item_id"], c["change"]) for c in k_inc.whats_new()["changes"]} >= {
        ("r:ors", "new"), ("p:1", "changed"), ("r:amf", "removed")}


def test_an_empty_delta_changes_no_content_and_a_second_one_stacks(tmp_path):
    v1 = _bundle(tmp_path / "v1")
    a = tmp_path / "a.sqlite"
    build(v1, a)
    same = tmp_path / "same.sqlite"
    assert apply(a, same, [])["applied"] == {"new": [], "changed": [], "content_changed": [], "removed": []}
    assert _dump(same) == _dump(a)
    # v1 -> v2 -> v1 again, all by deltas, is a full build of v1 (history aside)
    v2 = _v2(tmp_path)
    d, ups, parts, tables = _delta(v1, v2)
    b = tmp_path / "b.sqlite"
    apply(a, b, ups, d["removed"], manifest=parts["manifest"], aliases=parts["aliases"], tables=tables)
    d2, ups2, parts2, tables2 = _delta(v2, v1)
    c = tmp_path / "c.sqlite"
    apply(b, c, ups2, d2["removed"], manifest=parts2["manifest"], aliases=parts2["aliases"], tables=tables2)
    back = _dump(c)
    ref = _dump(a)
    for name in ("chunks", "sections", "cards", "cards_fts", "links", "entities", "item_entities", "grants", "fts"):
        assert back[name] == ref[name], name


def test_a_record_is_addressed_by_its_content_not_the_curators_paths(tmp_path):
    item = {"id": "x", "text": "t", "original": {"sha256": "1" * 64, "path": "/home/eva/raw/ab"}}
    elsewhere = {**item, "original": {"sha256": "1" * 64, "path": "/runner/raw/ab"}}
    assert records.sha(records.record(item)) == records.sha(records.record(elsewhere))
    assert "path" not in records.record(item)["original"]
    assert records.decode(records.encode(records.record(item)), records.sha(records.record(item)))["id"] == "x"
    assert records.encode({"a": 1}) == records.encode({"a": 1})


def test_a_first_version_built_from_records_is_the_full_build(tmp_path):
    """No machine builds kb.sqlite: a knowledge base's first version is its records
    applied to nothing, and that is the full build of the same content."""
    v2 = _v2(tmp_path)
    full = tmp_path / "full.sqlite"
    build(v2, full)
    texts = dict(records.texts(v2))
    recs = [records.join(rec, texts[rec["text_sha"]]) for _, _, rec in records.records(v2)]
    parts = records.parts(v2)
    tables = records.tables({n: records.decode(records.encode(v)) for n, v in parts.items()})
    first = tmp_path / "first.sqlite"
    s = apply(None, first, recs, manifest=parts["manifest"], aliases=parts["aliases"], tables=tables)
    assert sorted(s["applied"]["new"]) == sorted(r["id"] for r in recs) and s["baseline"] is True
    assert _dump(first) == _dump(full)
    assert KB(first).whats_new()["changes"] == KB(full).whats_new()["changes"]


def test_a_record_carries_its_text_by_reference(tmp_path):
    """A metadata or card change re-sends a small record; the text is sent once."""
    item = {"id": "x", "type": "page", "title": "T", "url": "u", "text": "long text " * 1000, "card": {"summary": "a"}}
    rec = records.record(item)
    assert "text" not in rec and rec["text_sha"] == records.sha(item["text"])
    recarded = records.record({**item, "card": {"summary": "b"}})
    assert records.sha(recarded) != records.sha(rec) and recarded["text_sha"] == rec["text_sha"]
    assert len(records.encode(rec)) < 200
    assert records.join(rec, item["text"]) == {k: v for k, v in item.items()}

