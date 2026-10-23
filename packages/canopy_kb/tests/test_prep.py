"""The curator's toolkit (`canopy_kb.prep`). The readers need the [prep] extra; those
tests skip without it, the rest are stdlib."""
import json

import pytest

from canopy_kb.prep import cards, dedupe, extract, originals


def _item(i, text, typ="page", url=None, **meta):
    return {"id": i, "type": typ, "title": i, "url": url or f"https://www.example.org/{i}", "date": "",
            "text": text, "meta": {"license": "x", **meta}}


def test_identical_texts_collapse_to_the_preferred_url_and_keep_every_referrer():
    a = _item("pdf:a", "Same text.", "pdf_document", "https://files.example.org/files/x.pdf", linked_from=["p1"])
    b = _item("pdf:b", "Same  text!", "pdf_document", "https://www.example.org/files/x.pdf", linked_from=["p2"])
    c = _item("pdf:c", "Other.", "pdf_document")
    final, dups = dedupe.collapse_duplicates([a, b, c], prefer=lambda it: "files." in it["url"])
    assert dups == 1 and [x["id"] for x in final] == ["pdf:b", "pdf:c"]
    assert final[0]["meta"]["also_at"] == ["https://files.example.org/files/x.pdf"]
    assert final[0]["meta"]["also_ids"] == ["pdf:a"] and final[0]["meta"]["linked_from"] == ["p1", "p2"]


def test_an_item_seen_twice_under_one_id_is_not_its_own_duplicate():
    a = _item("page:1", "Text.")
    final, dups = dedupe.collapse_duplicates([a, dict(a, meta={"license": "x"})])
    assert dups == 0 and len(final) == 1 and "also_ids" not in final[0]["meta"]


def test_a_tab_copied_into_several_workbooks_keeps_its_rows_in_the_most_linked_one():
    tab = "## Sheet: Moral weights\n" + "- Row 1 · value: 1\n" * 300
    own = "## Sheet: Main\n- Row 1 · own model\n\n"
    home = _item("sheet:home", own + tab, "cea_model", linked_from=["a", "b", "c"])
    copy = _item("sheet:copy", own.replace("own", "other") + tab, "cea_model", linked_from=["a"])
    assert dedupe.collapse_shared_tabs([home, copy]) == 1
    assert home["text"].count("- Row 1 · value: 1") == 300
    assert "Identical to the tab \"Moral weights\" in sheet:home" in copy["text"] and "value: 1" not in copy["text"]
    assert copy["meta"]["shared_tabs_in"] == ["sheet:home"]


def test_structured_items_are_carded_without_a_model_and_cards_attach_with_enrichment(tmp_path):
    (tmp_path / "items.jsonl").write_text(json.dumps(_item("grant:1", "Recipient: Dimagi", "grant_record")) + "\n")
    out = cards.run(tmp_path, "Acme", structured=lambda it: {"summary": it["text"]} if it["type"] == "grant_record" else None)
    assert out == {"written": 0, "failed": 0, "cached": 1}
    (tmp_path / "cards.json").write_text(json.dumps({
        "page:1": {"sha256": "x", "card": {"summary": "A review.", "orgs": ["Acme"]}},
        "sheet:1": {"sha256": "y", "card": {"summary": "A model.", "orgs": []}}}))
    items = [_item("page:1", "t", "charity_review"), _item("sheet:1:data", "t", "dataset", parent="sheet:1"), _item("x", "t")]
    assert cards.attach(items, tmp_path, enrich=lambda it, c: {**c, "orgs": c["orgs"] + ["from url"]}) == 2
    assert items[0]["card"]["orgs"] == ["Acme", "from url"]
    assert items[1]["card"]["summary"] == "Data tabs of: A model." and items[1]["card"]["kind"] == "model inputs/data"
    assert "card" not in items[2]


def test_each_format_goes_to_its_reader():
    ok = {"status": 200, "file": "f"}
    assert extract.kind("https://x/a", {**ok, "ctype": "text/html; charset=utf-8"}) == "page"
    assert extract.kind("https://x/a.pdf", {**ok, "ctype": "application/pdf"}) == "pdf"
    assert extract.kind("https://docs.google.com/document/d/abc/export?format=html", {**ok, "ctype": "text/html"}) == "gdoc"
    assert extract.kind("https://x/y.xls", {**ok, "ctype": "application/vnd.ms-excel"}) == "xls"
    assert extract.kind("https://x/y", {**ok, "ctype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}) == "xlsx"
    assert extract.kind("https://x", {"status": 404, "file": "f", "ctype": "text/html"}) is None
    assert extract.kind("https://x/i.png", {**ok, "ctype": "image/png"}) is None


def test_an_original_records_the_file_with_a_usable_name(tmp_path):
    row = {"url": "https://docs.google.com/spreadsheets/d/abc/export?format=xlsx", "status": 200, "sha256": "f" * 64,
           "bytes": 1234, "fetched_at": "2026-10-20T00:00:00Z", "cd": "attachment; filename=\"AMF CEA 2024.xlsx\"",
           "ctype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    o = originals.record(row, tmp_path / "ab", title="AMF model")
    assert o == {"sha256": "f" * 64, "filename": "AMF CEA 2024.xlsx", "bytes": 1234, "source_url": row["url"],
                 "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "fetched_at": "2026-10-20T00:00:00Z", "path": str(tmp_path / "ab")}
    page = {"url": "https://www.example.org/charities/amf", "ctype": "text/html; charset=utf-8"}
    assert originals.filename(page, "Against Malaria Foundation") == "Against Malaria Foundation.html"
    assert originals.filename({"url": "https://f.example.org/a/Report%202019.pdf", "ctype": "application/pdf"}) == "Report 2019.pdf"
    assert originals.record({**row, "status": 404}, tmp_path / "ab") is None


def test_page_extraction_keeps_the_whole_content_region_and_headings():
    pytest.importorskip("bs4")
    pytest.importorskip("markdownify")
    from canopy_kb.prep.html import page
    html = (b"<html><head><title>AMF | Acme</title></head><body><nav>menu</nav><div id='content-area'>"
            b"<h1>Against Malaria Foundation</h1><p>Intro.</p><h2>Cost</h2><ul><li>Net: $2</li></ul>"
            b"<h2>Later section</h2><p>Kept.</p></div><footer>f</footer></body></html>")
    title, md = page(html, title_suffix="| Acme")
    assert title == "AMF" and "# Against Malaria Foundation" in md and "## Later section" in md and "Kept." in md
    assert "menu" not in md and "Net: $2" in md


def test_shared_homes_on_descriptors_choose_what_collapse_shared_tabs_chooses():
    """A tie on links and length goes to the first copy, as the text-based pass does."""
    tab = "## Sheet: Moral weights\n" + "- Row 1 · value: 1\n" * 300
    a = _item("sheet:b", "## Sheet: Main\n- x\n\n" + tab, "cea_model", linked_from=["p"])
    b = _item("sheet:a", "## Sheet: Main\n- y\n\n" + tab, "cea_model", linked_from=["q"])
    descs = [{**it, "tabs": dedupe.tab_keys(it["text"]), "chars": len(it["text"])} for it in (a, b)]
    homes = dedupe.shared_homes(descs)
    dedupe.collapse_shared_tabs([a, b])
    assert list(homes.values()) == ["sheet:b"] and "sheet:b" in b["meta"]["shared_tabs_in"]
