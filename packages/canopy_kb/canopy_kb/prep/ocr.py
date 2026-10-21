"""Scanned PDFs (pages that are pictures, no text layer) transcribed by Gemini.

A PDF whose extracted text is empty on more than half its pages is sent whole to Gemini
2.5 Flash, which transcribes it verbatim as markdown. On GiveWell (2026-10-20) that was
272 of ~6,900 PDFs, nearly all reports charities sent in, 6.4M characters, a few dollars.
Results are cached in <data>/ocr.json by url with the file's sha256, so a refresh sends
only new or changed scans; `text_for` gives an assemble step the transcription to use.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import threading
from pathlib import Path

from canopy_kb.prep import gemini
from canopy_kb.prep.extract import KindFn, extracted, kind
from canopy_kb.prep.raw import Raw, log, read_json, write_json

PROMPT = ("Transcribe this scanned document to markdown, completely and verbatim, in reading order. "
          "Use # headings for headings and markdown tables for tables. Do not summarise or add anything.")
MAX_BYTES = 18_000_000  # inline request limit
EMPTY_SHARE = 0.5


def scanned(res: dict) -> bool:
    """An extracted PDF with no text, or with none on more than half its pages."""
    m = res.get("pdf") or {}
    return not (res.get("text") or "").strip() or bool(m.get("pages") and m.get("empty_pages", 0) / m["pages"] > EMPTY_SHARE)


def candidates(data: Path, raw: Raw, done: dict, kind_fn: KindFn = kind) -> list[tuple[str, dict]]:
    out = []
    for url, row in raw.index.items():
        if kind_fn(url, row) != "pdf" or (done.get(url) or {}).get("sha256") == row.get("sha256"):
            continue
        m = extracted(data, row, "pdf").get("pdf") or {}
        if m.get("pages") and m.get("empty_pages", 0) / m["pages"] > EMPTY_SHARE and (row.get("bytes") or 0) < MAX_BYTES:
            out.append((url, row))
    return out


def run(data: Path, workers: int = 8, limit: int | None = None, kind_fn: KindFn = kind) -> dict:
    raw = Raw(data)
    path = data / "ocr.json"
    done: dict = read_json(path, {})
    todo = candidates(data, raw, done, kind_fn)[:limit]
    log(f"ocr: {len(todo)} scanned PDFs to transcribe ({len(done)} cached)")
    lock = threading.Lock()
    failed = []

    def job(t):
        url, row = t
        try:
            text, _ = gemini.generate(gemini.FLASH, [
                {"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(raw.body(row)).decode()}},
                {"text": PROMPT}], json_out=False, max_tokens=65000)
        except Exception as e:  # noqa: BLE001
            failed.append(url)
            log("  ocr failed:", url[-80:], str(e)[:120])
            return
        with lock:
            done[url] = {"sha256": row.get("sha256"), "text": text}
            if len(done) % 25 == 0:
                write_json(path, done)

    with cf.ThreadPoolExecutor(workers) as ex:
        list(ex.map(job, todo))
    write_json(path, done)
    return {"transcribed": len(todo) - len(failed), "failed": len(failed), "cached": len(done)}


def text_for(ocr: dict, url: str, row: dict, res: dict) -> str | None:
    """The transcription to use in place of an extracted PDF's text, when it is a scan
    and the transcription is of these same bytes."""
    o = ocr.get(url)
    if o and o.get("sha256") == row.get("sha256") and (o.get("text") or "").strip() and scanned(res):
        return o["text"]
    return None
