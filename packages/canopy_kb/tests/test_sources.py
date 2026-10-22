"""What sources need from the engine: conditional refetches, the sensitivity checks a
Drive source's review runs, and a Drive file's `original`."""
import json

import pytest

from canopy_kb import KB, build, sensitive


def test_names_that_suggest_records_about_people_are_flagged_before_anything_is_read():
    assert sensitive.name_flags("Patient list March.xlsx")
    assert sensitive.name_flags("summary.docx", folders=["Kangaroo care", "Raw data"])
    assert sensitive.name_flags("Consent forms") and sensitive.name_flags("KMC line_list v2")
    assert sensitive.name_flags("Protocol.pdf", owner="someone@hospital.org", internal_domains=("dimagi.com",))
    assert sensitive.name_flags("Protocol.pdf", folders=["Kangaroo care"], owner="a@dimagi.com",
                                internal_domains=("dimagi.com",)) == []


def test_spreadsheet_headers_that_name_or_identify_a_person_hold_it():
    assert sensitive.header_flags(["Mother name", "DOB", "Phone", "Weight (g)", "Facility"]) == ["Mother name", "DOB", "Phone"]
    assert sensitive.header_flags(["Patient ID", "Date of birth"]) == ["Patient ID", "Date of birth"]
    assert sensitive.header_flags(["Indicator", "Program name", "Q1", "Q2", "File name"]) == []


def test_header_candidates_come_from_every_sheets_top_rows(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.append(["KMC register (synthetic)"])
    wb.active.append(["Baby name", "Birth weight"])
    wb.create_sheet("Notes").append(["Indicator", "Value"])
    p = tmp_path / "x.xlsx"
    wb.save(p)
    heads = sensitive.spreadsheet_headers(p, "xlsx")
    assert "Baby name" in heads and "Indicator" in heads
    assert sensitive.header_flags(heads) == ["Baby name"]
    (tmp_path / "x.csv").write_text("\nfirst_name,village\nA,B\n")
    assert sensitive.header_flags(sensitive.spreadsheet_headers(tmp_path / "x.csv", "csv")) == ["first_name"]


def test_a_refetch_is_conditional_and_a_304_keeps_the_bytes_and_the_record(tmp_path):
    httpx = pytest.importorskip("httpx")
    from canopy_kb.prep.raw import Raw
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"<html>v1</html>", headers={"etag": '"v1"', "content-type": "text/html"})

    raw = Raw(tmp_path)
    raw._local.c = httpx.Client(transport=httpx.MockTransport(handler))
    first = raw.get("https://x/p")
    again = raw.get("https://x/p", refetch=True)
    assert "if-none-match" not in seen[0] and seen[1]["if-none-match"] == '"v1"'
    assert again["sha256"] == first["sha256"] and again["fetched_at"] == first["fetched_at"] and again["checked_at"]
    assert raw.body(again) == b"<html>v1</html>"


def test_same_bytes_refetched_keep_their_fetched_at_so_the_record_does_not_change(tmp_path):
    httpx = pytest.importorskip("httpx")
    from canopy_kb.prep.raw import Raw
    raw = Raw(tmp_path)
    raw._local.c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"same")))
    first = raw.get("https://x/f")
    assert raw.get("https://x/f", refetch=True)["fetched_at"] == first["fetched_at"]


def test_a_drive_files_original_is_its_drive_link_at_the_indexed_revision(tmp_path):
    b = tmp_path / "b"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({"name": "kmc", "tables": {}}))
    (b / "items.jsonl").write_text(json.dumps({
        "id": "drive:abc", "type": "gdoc", "title": "Protocol", "url": "https://docs.google.com/document/d/abc",
        "text": "# Protocol\n\nSkin to skin.", "sha256": "t",
        "meta": {"source": "drive", "drive": {"file_id": "abc", "revision": "42"}},
        "original": {"sha256": "a" * 64, "filename": "Protocol.txt", "content_type": "text/plain", "bytes": 20,
                     "source_url": "https://docs.google.com/document/d/abc", "fetched_at": "2026-10-22T00:00:00Z"}}))
    build(b, tmp_path / "kb.sqlite")
    from canopy_kb.tools import run
    text = run(KB(tmp_path / "kb.sqlite"), "original", {"ref": "drive:abc"}, link=lambda *a: "https://s3/should-not")
    assert "revision 42" in text and "https://docs.google.com/document/d/abc" in text and "s3" not in text


def test_a_google_re_export_that_only_reshuffles_is_unchanged(tmp_path):
    """Google re-exports an unchanged Sheet with new comment-shape VML and a Doc with new
    class names; the kept copy and row stand, so the record does not change."""
    import io
    import zipfile

    httpx = pytest.importorskip("httpx")
    from canopy_kb.prep.raw import Raw

    def xlsx(vml: bytes, cells: bytes = b"<sheet>1</sheet>") -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", cells)
            z.writestr("xl/drawings/vmlDrawing1.vml", vml)
        return buf.getvalue()

    sheet = "https://docs.google.com/spreadsheets/d/x/export?format=xlsx"
    doc = "https://docs.google.com/document/d/y/export?format=html"
    ct = {sheet: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", doc: "text/html"}
    bodies = {sheet: [xlsx(b"id=1"), xlsx(b"id=2"), xlsx(b"id=3", b"<sheet>2</sheet>")],
              doc: [b'<style>.c1{}</style><p class="c1">Hi</p>', b'<style>.c7{}</style><p class="c7">Hi</p>',
                    b'<style>.c7{}</style><p class="c7">Bye</p>']}
    raw = Raw(tmp_path)
    raw._local.c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=bodies[str(r.url)].pop(0), headers={"content-type": ct[str(r.url)]})))
    for url in (sheet, doc):
        first = raw.get(url)
        same = raw.get(url, refetch=True)
        assert (same["sha256"], same["fetched_at"]) == (first["sha256"], first["fetched_at"])
        assert raw.body(same) == raw.body(first)
        assert raw.get(url, refetch=True)["sha256"] != first["sha256"]  # a real edit is a change


def test_aliases_are_not_asked_again_when_the_names_are_unchanged(tmp_path, monkeypatch):
    from canopy_kb.prep import aliases, gemini
    calls = []

    def ask(model, parts, **kw):
        calls.append(model)
        return [{"canonical": "Unlimit Health", "names": ["SCI Foundation", "Unlimit Health"]}], {}

    monkeypatch.setattr(gemini, "generate_json", ask)
    (tmp_path / "cards.json").write_text(json.dumps({"a": {"card": {"orgs": ["SCI Foundation", "Unlimit Health"]}}}))
    aliases.run(tmp_path, "GiveWell")
    aliases.run(tmp_path, "GiveWell")
    assert len(calls) == 2  # one per kind, once
    (tmp_path / "cards.json").write_text(json.dumps({"a": {"card": {"orgs": ["SCI Foundation", "Unlimit Health", "AMF"]}}}))
    aliases.run(tmp_path, "GiveWell")
    assert len(calls) == 4
