"""PDF -> markdown-ish text in reading order.

pdftotext -layout (the old extractor) prints a two-column page row by row, so a line holds
the left column's words followed by the right column's: every passage cut from such a page
mixes two texts. PyMuPDF's text blocks come in reading order, one block per paragraph or
cell; their lines are joined back into paragraphs (de-hyphenated), lines repeated at the
top or bottom of most pages (running headers, footers, page numbers) are dropped, and a
short block set in a larger font than the body becomes a heading.
"""
from __future__ import annotations

import collections
import re
import statistics

import pymupdf

BULLET = re.compile(r"^([•▪●◦‣\uf0b7\uf0a7\uf076\uf0d8\-–—*]|o\s|\(?[a-z0-9]{1,3}[.)]\s)")
PAGE_NUM = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$", re.I)


def _norm_line(s: str) -> str:
    return re.sub(r"\d+", "#", s.strip().lower())


def extract(path: str) -> tuple[str, dict]:
    doc = pymupdf.open(path)
    pages = []
    sizes: list[float] = []
    for page in doc:
        d = page.get_text("dict")  # content-stream order: a column is read top to bottom before the next
        blocks = []
        for b in d["blocks"]:
            if b.get("type") != 0:
                continue
            # a block can hold a whole page of a Word export: start a new paragraph where the
            # gap above a line is wider than the block's usual line spacing, or at a bullet
            rows = []
            for l in b["lines"]:
                t = "".join(s["text"] for s in l["spans"])
                if t.strip():
                    rows.append((l["bbox"], t, max((s["size"] for s in l["spans"] if s["text"].strip()), default=0)))
            if not rows:
                continue
            gaps = [rows[i][0][1] - rows[i - 1][0][3] for i in range(1, len(rows)) if rows[i][0][1] > rows[i - 1][0][1]]
            usual = statistics.median(gaps) if gaps else 0
            cur = [rows[0]]
            groups = [cur]
            for prev, row in zip(rows, rows[1:]):
                gap = row[0][1] - prev[0][3]
                if gap > max(usual * 1.8, usual + 3) or BULLET.match(row[1].strip()):
                    cur = [row]
                    groups.append(cur)
                else:
                    cur.append(row)
            for g in groups:
                blocks.append({"lines": [t for _, t, _ in g], "size": max(sz for _, _, sz in g),
                               "y0": g[0][0][1], "y1": g[-1][0][3], "h": page.rect.height})
                sizes += [sz for _, _, sz in g]
        pages.append(blocks)
    body = statistics.median(sizes) if sizes else 10
    # running headers/footers: first/last block lines that recur on most pages
    edge = collections.Counter()
    for blocks in pages:
        seen = set()
        for b in blocks:
            if b["y0"] < b["h"] * 0.08 or b["y1"] > b["h"] * 0.93:
                for ln in b["lines"]:
                    seen.add(_norm_line(ln))
        edge.update(seen)
    n = len(pages)
    drop = {k for k, v in edge.items() if n >= 4 and v >= max(3, 0.5 * n)}
    out = []
    empty_pages = 0
    for blocks in pages:
        chars = 0
        for b in blocks:
            lines = [ln for ln in b["lines"] if _norm_line(ln) not in drop and not (PAGE_NUM.match(ln) and (b["y0"] < b["h"] * 0.1 or b["y1"] > b["h"] * 0.9))]
            if not lines:
                continue
            text = ""
            for ln in lines:
                ln = ln.strip()
                if not text:
                    text = ln
                elif text.endswith("-") and ln[:1].islower():
                    text = text[:-1] + ln
                elif BULLET.match(ln):
                    text += "\n" + ln  # a list item starts its own line
                else:
                    text += " " + ln
            text = re.sub(r"[ \t]{2,}", " ", text)
            chars += len(text)
            if out and text[:1].islower() and not out[-1].startswith("## ") and not re.search(r"[.:;!?]$", out[-1]):
                out[-1] += " " + text  # a wrapped line that landed in its own block
            elif b["size"] >= body * 1.2 and len(text) < 150 and "\n" not in text and not text.endswith("."):
                out.append(f"## {text}")
            else:
                out.append(text)
        if chars < 80:
            empty_pages += 1
    meta = {"pages": n, "empty_pages": empty_pages}
    return "\n\n".join(out), meta


if __name__ == "__main__":
    import sys
    t, m = extract(sys.argv[1])
    print(m)
    print(t)
