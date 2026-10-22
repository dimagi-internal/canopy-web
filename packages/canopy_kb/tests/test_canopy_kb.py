"""The engine and its MCP tool layer, with plain pytest (no Django)."""
import json

import pytest

from canopy_kb import KB, KBError, build
from canopy_kb.chunking import chunk, outline
from canopy_kb.tools import run, tool_specs

REVIEW = ("# Against Malaria Foundation\n\nAMF funds bed nets.\n\n## Cost-effectiveness\n\n"
          + "Each net costs about $2 to buy and distribute. " * 120 + "\n\n## Monitoring\n\nPost-distribution surveys.")


def _bundle(tmp_path, items=None, aliases=None):
    b = tmp_path / "bundle"
    (b / "tables").mkdir(parents=True)
    (b / "manifest.json").write_text(json.dumps({
        "name": "acme", "publisher": "Acme Foundation", "license": "CC BY 4.0",
        "guide": ["Grants table amounts beat prose."],
        "tables": {"grants": {"description": "Every grant.", "date_column": "Date",
                              "amount_column": "Amount", "filter_columns": ["Recipient"]}}}))
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in items or [
        {"id": "p:1", "type": "podcast_episode", "title": "Diarrheal disease", "url": "https://x/1",
         "date": "2026-07-23", "text": "Cholera spreads through contaminated water.", "sha256": "a",
         "card": {"summary": "A podcast on cholera and water.", "kind": "Podcast_Transcript",
                  "orgs": ["Dimagi Inc"], "programs": ["water chlorination"]}},
        {"id": "b:1", "type": "blog_post", "title": "Safe water", "url": "https://x/2",
         "date": "2025-10-29", "text": "Chlorination pilots in six countries.", "sha256": "b",
         "meta": {"linked_from": ["https://x/1"]},
         "card": {"summary": "Chlorination pilots.", "orgs": ["Dimagi"], "programs": ["in-line chlorination"]}},
        {"id": "r:amf", "type": "charity_review", "title": "Against Malaria Foundation – November 2021 version",
         "url": "https://x/amf-2021", "date": "2021-11", "text": REVIEW, "sha256": "c",
         "card": {"summary": "GiveWell's 2021 review of AMF.", "orgs": ["Against Malaria Foundation (AMF)"]}},
        {"id": "r:amf2", "type": "charity_review", "title": "Against Malaria Foundation – December 2023 version",
         "url": "https://x/amf-2023", "date": "2023-12", "text": "Newer review.", "sha256": "d",
         "card": {"summary": "The 2023 review.", "orgs": ["AMF"]}},
    ]))
    (b / "aliases.json").write_text(json.dumps(aliases or {
        "program": [{"canonical": "Water chlorination", "names": ["water chlorination", "in-line chlorination"]}]}))
    (b / "tables" / "grants.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"Grant": "Water pilot", "Recipient": "Dimagi Inc", "Amount": 1069324, "Date": "2025-12-03", "url": "https://g/1"},
        {"Grant": "Cholera RCT", "Recipient": "Johns Hopkins", "Amount": 740851.8, "Date": "2025-03-14", "url": "https://g/2"},
    ]))
    return b


@pytest.fixture()
def kb(tmp_path):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path), db)
    return KB(db)


def test_passages_cover_the_text_never_cut_a_word_and_carry_their_heading():
    ps = chunk(REVIEW, size=800, overlap=100)
    assert len(ps) > 3
    covered = set()
    for p in ps:
        covered.update(range(p.start, p.end))
        assert p.start == 0 or not (REVIEW[p.start - 1].isalnum() and REVIEW[p.start].isalnum())
        assert p.end == len(REVIEW) or not (REVIEW[p.end - 1].isalnum() and REVIEW[p.end].isalnum())
    assert all(i in covered for i, ch in enumerate(REVIEW) if not ch.isspace())
    assert any(p.heading == "Against Malaria Foundation › Cost-effectiveness" for p in ps)
    assert [s.path for s in outline(REVIEW)][-1] == "Against Malaria Foundation › Monitoring"


def test_an_items_text_is_rebuilt_exactly_from_its_overlapping_passages(kb):
    assert kb.text("r:amf").strip() == REVIEW.strip()
    a = REVIEW.index("## Monitoring")
    assert kb.text("r:amf", a, a + 13) == "## Monitoring"
    assert kb.get("r:amf")["text"].strip() == REVIEW.strip()


def test_cards_feed_one_entity_per_thing_through_acronyms_and_aliases(kb):
    out = kb.entity("AMF")
    assert out.startswith("Against Malaria Foundation (org) — 2 documents")
    assert "r:amf2" in out and "r:amf" in out
    water = kb.entity("in-line chlorination")
    assert water.startswith("Water chlorination (program) — 2 documents")
    dimagi = kb.entity("Dimagi")
    assert "grants table (1 rows, 1,069,324 total Amount)" in dimagi and "https://g/1" in dimagi


def test_doc_shows_outline_links_and_other_versions(kb):
    d = kb.doc("r:amf")
    assert "Cost-effectiveness" in d and "outline (" in d
    assert "other versions (same title):" in d and "r:amf2" in d
    assert "linked from (1):" in kb.doc("b:1") and "p:1" in kb.doc("b:1")


def test_read_a_section_and_continue_from_an_offset(kb):
    sec = next(i for i, s in enumerate(outline(REVIEW)) if s.path.endswith("Monitoring"))
    assert "Post-distribution surveys." in kb.read("r:amf", section=sec)
    first = kb.read("r:amf", chars=600)
    assert "[continue with at=" in first


def test_grep_finds_lines_with_offsets_in_one_document_or_an_entitys(kb):
    assert "(at " in kb.grep(r"\$2 to buy", ref="r:amf")
    assert "r:amf2" in kb.grep("newer", entity="AMF")


def test_sql_reads_declared_tables_and_refuses_writes(kb):
    out = kb.sql("SELECT Recipient, Amount FROM grants ORDER BY Amount DESC")
    assert out.splitlines()[1].startswith("Dimagi Inc | 1069324")
    assert kb.sql("DELETE FROM grants").startswith("Only SELECT")
    assert kb.sql("SELECT nope FROM grants").startswith("SQL error")


def test_search_returns_documents_with_cards_and_offsets(kb):
    out = kb.find("cholera water")
    assert out.startswith("[1] p:1 · podcast_episode")
    assert "card (podcast transcript): A podcast on cholera and water." in out and "(at 0)" in out
    assert "b:1" in kb.find("pilots", entity="water chlorination")
    assert kb.find("pilots", types=["podcast_episode"]).startswith("No matches")


def test_the_tools_are_the_research_set_plus_tables(kb):
    names = [t["name"] for t in tool_specs(kb)]
    assert names[:7] == ["kb_info", "search", "entity", "doc", "read", "grep", "sql"]
    assert "grants" in names and "get" in names
    assert "Grants table amounts beat prose." in run(kb, "kb_info", {})
    assert run(kb, "grants", {"Recipient": "Dimagi"})["rows"][0]["Grant"] == "Water pilot"
    assert "Post-distribution" in run(kb, "read", {"ref": "https://x/amf-2021", "at": REVIEW.index("Post")})


def test_an_unknown_tool_is_refused(kb):
    with pytest.raises(KBError):
        run(kb, "drop_tables", {})


def test_an_older_schema_file_asks_for_a_rebuild(tmp_path):
    import sqlite3
    p = tmp_path / "old.sqlite"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('summary', '{\"schema\": 2}')")
    db.commit()
    with pytest.raises(KBError, match="rebuild"):
        KB(p)


def test_a_rebuild_reports_what_is_new_and_changed(tmp_path):
    first = tmp_path / "a.sqlite"
    build(_bundle(tmp_path), first)
    b = _bundle(tmp_path / "two", items=[
        {"id": "p:1", "type": "podcast_episode", "title": "Diarrheal disease", "url": "https://x/1",
         "date": "2026-07-23", "text": "Changed.", "sha256": "z"},
        {"id": "n:1", "type": "blog_post", "title": "New", "url": "https://x/9", "date": "2026-10-01",
         "text": "Brand new.", "sha256": "n"}])
    second = tmp_path / "b.sqlite"
    s = build(b, second, previous=first)
    assert (s["new"], s["changed"], s["removed"]) == (1, 1, 3)
    assert {c["change"] for c in KB(second).whats_new()["changes"]} == {"new", "changed", "removed"}
