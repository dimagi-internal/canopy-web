"""Passages and outlines of an item's markdown text.

A passage is a span [start, end) of the item's text plus the heading path it sits
under ("Main CEA › Costs"). The span is all the file stores (the text is stored once,
on the item), so overlap between neighbouring passages costs no space. Cuts fall, in
order of preference, before a heading, at a blank line, at a line end, after a
sentence, at a space; never inside a word. A long markdown table is split by rows,
each later passage noting the table's header row in its heading.

Measured on the GiveWell corpus (1,072 questions, passage that contains the answer
in the top 10): 3,200-char passages with 400 of overlap and the heading path beat
1,800-char blind cuts (687 vs 576) — the old chunker cut half of all passages
mid-word because PDF and HTML text has single newlines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HEADING_M = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
SIZE = 3200
OVERLAP = 400
#: Cut before a heading once a passage is this full, so sections start passages.
MIN_FILL = 0.5
_CUTS = [re.compile(r"\n"), re.compile(r"(?<=[.!?;])\s+"), re.compile(r"(?<=,)\s+"), re.compile(r"\s+")]
_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


@dataclass(frozen=True)
class Passage:
    start: int
    end: int
    heading: str


@dataclass(frozen=True)
class Section:
    level: int
    path: str
    start: int
    end: int


def _clean(h: str) -> str:
    return re.sub(r"[*_`]+", "", h).strip()[:120]


def _blocks(text: str) -> list[tuple[int, int, str, int, str]]:
    """(start, end, kind, heading level, heading text) per paragraph; a heading line is
    always a block of its own."""
    out = []
    pos = 0
    for m in re.finditer(r"\n[ \t]*\n+", text + "\n\n"):
        s, e = pos, m.start()
        pos = m.end()
        if s >= len(text):
            break
        seg = text[s:e]
        if not seg.strip():
            continue
        off, cur = s, None
        for ln in seg.split("\n"):
            hm = HEADING.match(ln)
            if hm:
                if cur is not None:
                    out.append((cur, off - 1, "text", 0, ""))
                    cur = None
                out.append((off, off + len(ln), "heading", len(hm.group(1)), _clean(hm.group(2))))
            elif cur is None:
                cur = off
            off += len(ln) + 1
        if cur is not None:
            out.append((cur, min(e, len(text)), "text", 0, ""))
    return out


def _split_long(text: str, s: int, e: int, size: int, level: int = 0) -> list[tuple[int, int]]:
    """Spans of at most `size` inside [s, e), cut at the coarsest boundary there is."""
    if e - s <= size:
        return [(s, e)] if text[s:e].strip() else []
    if level >= len(_CUTS):
        return [(k, min(e, k + size)) for k in range(s, e, size)]
    seg = text[s:e]
    cuts = [m.end() for m in _CUTS[level].finditer(seg) if 0 < m.end() < len(seg)]
    if not cuts:
        return _split_long(text, s, e, size, level + 1)
    joined, a0, b0 = [], None, None
    prev = 0
    for c in cuts + [len(seg)]:  # greedy: join pieces while they fit
        if a0 is None:
            a0, b0 = prev, c
        elif c - a0 <= size:
            b0 = c
        else:
            joined.append((a0, b0))
            a0, b0 = prev, c
        prev = c
    joined.append((a0, b0))
    out = []
    for a, b in joined:
        if b - a > size:
            out += _split_long(text, s + a, s + b, size, level + 1)
        elif text[s + a:s + b].strip():
            out.append((s + a, s + b))
    return out


def chunk(text: str, size: int = SIZE, overlap: int = OVERLAP) -> list[Passage]:
    """The item's passages, covering every non-blank character."""
    text = text or ""
    if not text.strip():
        return []
    units: list[tuple[int, int, str, bool]] = []  # start, end, heading path, is a heading
    path: list[tuple[int, str]] = []
    for s, e, kind, lvl, htxt in _blocks(text):
        if kind == "heading":
            path = [p for p in path if p[0] < lvl] + [(lvl, htxt)]
            units.append((s, e, " › ".join(h for _, h in path), True))
            continue
        hp = " › ".join(h for _, h in path)
        seg = text[s:e]
        if seg.lstrip().startswith("|") and e - s > size:  # a long table: one unit per row
            lines = seg.split("\n")
            head = lines[0][:200]
            if len(lines) > 1 and _TABLE_RULE.match(lines[1]):
                head = lines[0][:200]
            off = s
            for ln in lines:
                units.append((off, off + len(ln), hp + (f" | table: {head}" if off > s else ""), False))
                off += len(ln) + 1
            continue
        units += [(a, b, hp, False) for a, b in _split_long(text, s, e, size)]
    passages: list[Passage] = []
    i, n = 0, len(units)
    while i < n:
        start = units[i][0]
        heading = units[i][2] if not units[i][3] else " › ".join(units[i][2].split(" › ")[:-1])
        j, end = i, units[i][1]
        while j + 1 < n:
            nxt = units[j + 1]
            if nxt[1] - start > size:
                break
            if nxt[3] and end - start >= size * MIN_FILL:
                break
            j += 1
            end = nxt[1]
        passages.append(Passage(start, end, heading))
        if j + 1 >= n:
            break
        # the next passage repeats the trailing units that fit in `overlap`, never a heading's tail
        k = j + 1
        if not units[k][3]:
            back = j
            while back > i and end - units[back][0] <= overlap and not units[back][3]:
                back -= 1
            if back + 1 <= j and end - units[back + 1][0] <= overlap:
                k = back + 1
        i = max(k, i + 1)
    return passages


def outline(text: str) -> list[Section]:
    """Every heading as a section: its path and its span, up to the next heading."""
    hs = [(len(m.group(1)), _clean(m.group(2)), m.start()) for m in HEADING_M.finditer(text or "")]
    out, path = [], []
    if hs and hs[0][2] > 200:
        out.append(Section(1, "(start)", 0, hs[0][2]))
    for i, (lvl, h, s) in enumerate(hs):
        path = [p for p in path if p[0] < lvl] + [(lvl, h)]
        e = hs[i + 1][2] if i + 1 < len(hs) else len(text)
        out.append(Section(lvl, " › ".join(x for _, x in path), s, e))
    return out
