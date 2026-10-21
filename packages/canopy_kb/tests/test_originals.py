"""Original files: kept beside the text, versioned across builds, handed out by `original`."""
import json

from canopy_kb import KB, build
from canopy_kb.tools import run, tool_specs

PDF_V1 = {"sha256": "1" * 64, "filename": "AMF PDM 2019.pdf", "content_type": "application/pdf", "bytes": 2_400_000,
          "source_url": "https://files.example/amf.pdf", "fetched_at": "2026-10-01T00:00:00Z", "path": "/curator/raw/ab"}
PDF_V2 = {**PDF_V1, "sha256": "2" * 64, "bytes": 2_500_000, "fetched_at": "2026-10-20T00:00:00Z"}


def _bundle(tmp_path, original):
    b = tmp_path / "bundle"
    b.mkdir(exist_ok=True)
    (b / "manifest.json").write_text(json.dumps({"name": "acme", "publisher": "Acme", "tables": {}}))
    items = [{"id": "pdf:amf", "type": "pdf_document", "title": "AMF post-distribution monitoring", "url": PDF_V1["source_url"],
              "date": "2019", "text": "# Results\n\nNets in use: 74%.", "sha256": original["sha256"] if original else "t"},
             {"id": "blog:1", "type": "blog_post", "title": "A post", "url": "https://x/1", "date": "2026",
              "text": "Text only.", "sha256": "b"}]
    if original:
        items[0]["original"] = original
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    return b


def _kb(tmp_path, name, original, previous=None):
    db = tmp_path / name
    build(_bundle(tmp_path, original), db, previous)
    return KB(db)


def test_an_item_keeps_its_original_without_the_curators_local_path(tmp_path):
    kb = _kb(tmp_path, "kb.sqlite", PDF_V1)
    [o] = kb.originals("pdf:amf")
    assert o["sha256"] == PDF_V1["sha256"] and o["filename"] == "AMF PDM 2019.pdf" and o["superseded_at"] == ""
    assert "path" not in o and "/curator/" not in (tmp_path / "kb.sqlite").read_bytes().decode("latin-1")
    assert kb.originals("blog:1") == []
    assert kb.summary["originals"] == {"current": 1, "superseded": 0, "files": 1, "bytes": 2_400_000}


def test_a_changed_file_keeps_the_earlier_version_and_a_removed_one_stays_listed(tmp_path):
    v1 = _kb(tmp_path, "v1.sqlite", PDF_V1)
    v2 = _kb(tmp_path, "v2.sqlite", PDF_V2, previous=v1.path)
    now, before = v2.originals("pdf:amf")
    assert (now["sha256"], now["superseded_at"]) == (PDF_V2["sha256"], "")
    assert before["sha256"] == PDF_V1["sha256"] and before["superseded_at"]
    # the publisher took the file down: the item's text stays, so do both versions
    v3 = _kb(tmp_path, "v3.sqlite", None, previous=v2.path)
    assert [o["sha256"] for o in v3.originals("pdf:amf")] == [PDF_V2["sha256"], PDF_V1["sha256"]]
    assert all(o["superseded_at"] for o in v3.originals("pdf:amf"))
    assert v3.originals("pdf:amf")[1]["superseded_at"] == before["superseded_at"]  # first superseded, not re-dated


def test_the_original_tool_hands_out_a_signed_link_and_lists_versions(tmp_path):
    v1 = _kb(tmp_path, "v1.sqlite", PDF_V1)
    kb = _kb(tmp_path, "v2.sqlite", PDF_V2, previous=v1.path)
    assert "original" in {t["name"] for t in tool_specs(kb)}
    signed = []

    def link(sha, filename, ctype):
        signed.append((sha, filename, ctype))
        return f"https://store/{sha[:4]}?sig=x"

    out = run(kb, "original", {"ref": "pdf:amf"}, link=link)
    assert "download (valid 15 minutes): https://store/2222?sig=x" in out and "versions (pass version=N)" in out
    assert signed == [(PDF_V2["sha256"], "AMF PDM 2019.pdf", "application/pdf")]
    assert "https://store/1111" in run(kb, "original", {"ref": "pdf:amf", "version": 1}, link=link)
    assert "the publisher's copy" in run(kb, "original", {"ref": "pdf:amf"})  # a host that cannot sign
    assert "no original file" in run(kb, "original", {"ref": "blog:1"}, link=link)
    doc = run(kb, "doc", {"ref": "pdf:amf"})
    assert "original: AMF PDM 2019.pdf · application/pdf · 2.5 MB" in doc and "1 earlier version" in doc
    assert "`original` hands you that file" in run(kb, "kb_info", {})


def test_a_library_without_originals_offers_no_original_tool(tmp_path):
    kb = _kb(tmp_path, "kb.sqlite", None)
    assert "original" not in {t["name"] for t in tool_specs(kb)}
    assert "original:" not in run(kb, "doc", {"ref": "pdf:amf"})
