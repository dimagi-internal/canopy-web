"""Raw bytes -> markdown, one reader per format, cached by the file's sha256.

`kind` says which reader a fetched file needs, from its content type; `extract_all`
reads every raw file not already in the cache (<data>/extract/), in parallel, so a
refresh re-reads only what changed; `extracted` returns one cached result. A curator's
assemble step turns the results into items (its ids, types and titles are its own).

Formats: HTML pages (a whole content region to markdown), PDFs (PyMuPDF in reading
order; `ocr` for scans), xlsx and xls (row-per-line with headers, number formats,
formulas and notes; large record tabs returned separately as `data`), Google Docs
(exported html), docx, and Word 97 .doc (macOS `textutil`, where it exists).
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from canopy_kb.prep.raw import Raw, log, read_json

#: Bump to re-extract everything after a reader changes.
EXTRACTOR_VERSION = "2"

#: Which reader a fetched file needs: (url, raw index row) -> kind, or None to skip it.
KindFn = Callable[[str, dict], Optional[str]]


def kind(url: str, row: dict) -> str | None:
    """The reader for a fetched file, from its content type. HTML is "page" (a curator
    narrows which hosts' pages it keeps with its own KindFn)."""
    if row.get("status") != 200 or not row.get("file"):
        return None
    ct = (row.get("ctype") or "").split(";")[0].strip()
    if "docs.google.com/document" in url and "html" in ct:
        return "gdoc"
    if "html" in ct:
        return "page"
    return {"application/pdf": "pdf", "application/msword": "doc", "application/vnd.ms-excel": "xls"}.get(ct) or (
        "xlsx" if "spreadsheetml" in ct else "docx" if "wordprocessingml" in ct else None)


def xls_to_xlsx(path: Path, out: Path) -> None:
    import openpyxl
    import xlrd
    from xlrd.xldate import xldate_as_datetime
    book = xlrd.open_workbook(file_contents=path.read_bytes(), formatting_info=False)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sh in book.sheets():
        ws = wb.create_sheet(sh.name[:31] or "Sheet")
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                v = cell.value
                if cell.ctype == xlrd.XL_CELL_EMPTY or v == "":
                    continue
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        v = xldate_as_datetime(v, book.datemode)
                    except Exception:  # noqa: BLE001
                        pass
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    v = "#ERR"
                ws.cell(r + 1, c + 1, v)
    wb.save(out)


def extract_one(kind_: str, path: str, page_selector: str | None = None, title_suffix: str = "") -> dict:
    """Read one raw file. Never raises: a failure is a result with `error`."""
    p = Path(path)
    try:
        if kind_ == "page":
            from canopy_kb.prep.html import CONTENT, page
            title, md = page(p.read_bytes(), page_selector or CONTENT, title_suffix)
            return {"title": title, "text": md}
        if kind_ == "pdf":
            import pymupdf
            from canopy_kb.prep.pdf import extract
            text, meta = extract(str(p))
            mt = (pymupdf.open(str(p)).metadata or {}).get("title", "") or ""
            return {"text": text, "pdf_meta_title": mt, "pdf": meta}
        if kind_ in ("xlsx", "xls"):
            from canopy_kb.prep.xlsx import render
            if kind_ == "xls":
                with tempfile.TemporaryDirectory() as td:
                    x = Path(td) / "book.xlsx"
                    xls_to_xlsx(p, x)
                    text, sheets, links, data = render(x)
            else:
                text, sheets, links, data = render(p)
            return {"text": text, "sheets": sheets, "cell_links": links, "data": data}
        if kind_ == "gdoc":
            from canopy_kb.prep.html import to_markdown
            return {"text": to_markdown(p.read_bytes())[1]}
        if kind_ == "doc":
            from canopy_kb.prep.html import to_markdown
            if not shutil.which("textutil"):
                return {"error": "no textutil here (macOS) to read Word 97 .doc"}
            with tempfile.TemporaryDirectory() as td:
                src, dst = Path(td) / "in.doc", Path(td) / "out.html"
                src.write_bytes(p.read_bytes())
                subprocess.run(["textutil", "-convert", "html", str(src), "-output", str(dst)],
                               capture_output=True, timeout=120, check=True)
                return {"text": to_markdown(dst.read_bytes())[1]}
        if kind_ == "docx":
            import docx
            d = docx.Document(str(p))
            parts = []
            for para in d.paragraphs:
                if para.text.strip():
                    lvl = para.style.name.replace("Heading ", "") if para.style.name.startswith("Heading") else ""
                    parts.append(("#" * int(lvl) + " " if lvl.isdigit() else "") + para.text.strip())
            for t in d.tables:
                for r in t.rows:
                    parts.append("| " + " | ".join(c.text.strip() for c in r.cells) + " |")
            return {"text": "\n\n".join(parts)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"error": f"no reader for {kind_}"}


def cache_path(data: Path, row: dict, kind_: str) -> Path:
    return data / "extract" / f"{row['sha256'][:2]}" / f"{row['sha256']}-{kind_}-v{EXTRACTOR_VERSION}.json"


def _job(args: tuple) -> None:
    kind_, src, out, selector, suffix = args
    res = extract_one(kind_, src, selector, suffix)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def extract_all(data: Path, kind_fn: KindFn = kind, workers: int = 8, page_selector: str | None = None,
                title_suffix: str = "", raw: Raw | None = None) -> dict:
    """Extract every raw file not already in the cache."""
    raw = raw or Raw(data)
    jobs, kinds, queued = [], collections.Counter(), set()
    for url, row in raw.index.items():
        k = kind_fn(url, row)
        if not k:
            continue
        kinds[k] += 1
        out = cache_path(data, row, k)
        if not out.exists() and out not in queued:  # the same bytes at two urls are read once
            queued.add(out)
            jobs.append((k, str(raw.path(row)), str(out), page_selector, title_suffix))
    log(f"extract: {sum(kinds.values())} files ({dict(kinds)}); {len(jobs)} not yet extracted")
    with cf.ProcessPoolExecutor(workers) as ex:
        for n, _ in enumerate(ex.map(_job, jobs, chunksize=4), 1):
            if n % 500 == 0:
                log(f"  extracted {n}/{len(jobs)}")
    return {"files": sum(kinds.values()), "extracted": len(jobs)}


def extracted(data: Path, row: dict, kind_: str) -> dict:
    return read_json(cache_path(data, row, kind_), {"error": "not extracted"})
