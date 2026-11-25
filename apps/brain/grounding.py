"""Quote grounding at emit: every piece of evidence an output gives is a quote of a row the run actually read.

An output's `body.evidence` is a list of `{source, index, quote}`: the source id the run was served (`brain_read`), the
message's `index` and a short verbatim quote of it. `check` holds each item to the row it names, read again the way the
run read it (`reading.view_of`, scrubbed: the text `brain_read` served):

* the source must be one this run read (`BrainRunRead`), and the row one it was served: at or before how far it read
  (`BrainRunRead.through`; an earlier row of a session the learner read before is still that session's words);
* the quote must be in that row once both are folded (`fold`): case, runs of whitespace, curly quotes and dashes,
  inline markdown marks and quotation marks do not count, and an ellipsis (`…`, `...`, `[...]`) stands for words left
  out, so each piece around it must be there in order. A quote may be any part of the row, never more than it.

A missing `index` is found: the first row the run read of that source that holds the quote, which `check` writes into
the item. A failure names the item (`evidence[2]`), its source and row, and why, and where the quote is when it is in
another row the run read, so the run fixes it and emits again in the same run; nothing is written until every item
holds. The sources evidence names are cited by the output (`emit` adds them)."""
from __future__ import annotations

import re
import unicodedata

from apps.canopy_sessions import transcript_read

from .models import BrainRun, BrainRunRead, BrainSource
from .reading import view_of

#: How many pieces of evidence an output may give.
MAX_EVIDENCE = 30
#: The shortest quote (folded) that grounds anything: a word or two match everywhere. A row shorter than this is
#: quoted whole.
MIN_QUOTE_CHARS = 8
#: How many rows of a source are searched for a quote whose index is missing or wrong.
SEARCH_ROWS = 2000

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = dict.fromkeys(map(ord, "\"'`‘’‚‛“”„‟«»‹›ʼ"), "")
_MARKS = dict.fromkeys(map(ord, "*~"), "")
_ELLIPSIS = re.compile(r"\[\s*(?:\.\s*){3,}\]|\[\s*…\s*\]|…|(?:\.\s*){3,}")
_SPACE = re.compile(r"\s+")


class GroundingError(ValueError):
    """An evidence item does not hold against what the run read, with which and why."""


def fold(text: str) -> str:
    """`text` as quotes are compared: NFKC, case-folded, dashes as `-`, quotation and inline-markdown marks dropped,
    whitespace runs one space."""
    s = unicodedata.normalize("NFKC", text or "").casefold().translate(_DASHES).translate(_QUOTES).translate(_MARKS)
    return _SPACE.sub(" ", s).strip()


def pieces(quote: str) -> list[str]:
    """A quote's folded pieces: the parts an ellipsis separates, empty ones left out."""
    return [p for p in (fold(part) for part in _ELLIPSIS.split(quote or "")) if p]


def holds(quote: str, row_text: str) -> bool:
    """Whether `quote` is in `row_text`: each piece there, in order (`pieces`)."""
    parts, text = pieces(quote), fold(row_text)
    if not parts:
        return False
    at = 0
    for p in parts:
        found = text.find(p, at)
        if found < 0:
            return False
        at = found + len(p)
    return True


def too_short(quote: str, row_text: str) -> bool:
    """A quote too short to ground anything, unless it is the whole row."""
    got = " ".join(pieces(quote))
    return len(got) < MIN_QUOTE_CHARS and got != fold(row_text)


def _rows(run: BrainRun, source: BrainSource, *, after: int | None, limit: int):
    if source.session is None:
        return []
    return transcript_read.read(source.session, view=view_of(run), scrub=True, after=after, limit=limit).messages


def _short(text: str, n: int = 160) -> str:
    text = _SPACE.sub(" ", text or "").strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def _where(run: BrainRun, source: BrainSource, quote: str, through: int | None) -> dict | None:
    """The first row of `source` the run read that holds `quote`, or None."""
    for row in _rows(run, source, after=None, limit=SEARCH_ROWS):
        if through is not None and row["index"] > through:
            break
        if holds(quote, row["text"]):
            return row
    return None


def check(run: BrainRun, evidence) -> tuple[list[dict], set[int]]:
    """Hold every item of `evidence` to the row it names; return the items (each `index` filled in) and the source ids
    they name. Raises `GroundingError` naming the first item that fails, and why."""
    if not isinstance(evidence, list):
        raise GroundingError("evidence is a list of {source, index, quote}")
    if len(evidence) > MAX_EVIDENCE:
        raise GroundingError(f"give at most {MAX_EVIDENCE} pieces of evidence")
    ids = set()
    for i, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise GroundingError(f"evidence[{i}] is not an object: give {{source, index, quote}}, the quote verbatim "
                                 "from a message you read")
        src = item.get("source")
        if isinstance(src, bool) or not isinstance(src, int | str) or not str(src).isdecimal():
            raise GroundingError(f"evidence[{i}] names no source: give the `source` id brain_read served it under")
        ids.add(int(src))
    reads = {r.source_id: r for r in BrainRunRead.objects.filter(run=run, source_id__in=ids).select_related(
        "source__session")}
    out = []
    for i, item in enumerate(evidence):
        src, quote, index = int(item["source"]), item.get("quote"), item.get("index")
        name = f"evidence[{i}] (source {src}" + (f", message {index})" if index is not None else ")")
        read = reads.get(src)
        if read is None:
            raise GroundingError(f"{name}: this run did not read source {src}; cite only sources brain_read served "
                                 "you in this run")
        if not isinstance(quote, str) or not pieces(quote):
            raise GroundingError(f"{name}: give the `quote`, verbatim from that message")
        if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
            raise GroundingError(f"{name}: `index` is the message's index, a whole number")
        through = read.through
        if through is not None and through < 0:
            raise GroundingError(f"{name}: this run was served no message of source {src}; cite a message it read")
        if index is None:
            found = _where(run, read.source, quote, through)
            if found is None:
                raise GroundingError(f"{name}: the quote is in no message of it this run read; quote the words as "
                                     "they are written")
            if too_short(quote, found["text"]):
                raise GroundingError(f"{name}: the quote is too short to show anything; quote at least a phrase")
            out.append({**item, "source": src, "index": found["index"]})
            continue
        if index < 0 or (through is not None and index > through):
            raise GroundingError(f"{name}: this run read source {src} only through message {through}; cite a message "
                                 "it read")
        rows = _rows(run, read.source, after=index - 1, limit=1)
        row = rows[0] if rows and rows[0]["index"] == index else None
        if row is None:
            raise GroundingError(f"{name}: source {src} has no message {index} in what this run read")
        if not holds(quote, row["text"]):
            elsewhere = _where(run, read.source, quote, through)
            hint = (f"it is in message {elsewhere['index']}: cite that index" if elsewhere is not None else
                    f"message {index} reads: \"{_short(row['text'])}\"; quote its words as they are written (an "
                    "ellipsis for words left out), not a paraphrase")
            raise GroundingError(f"{name}: the quote is not in that message; {hint}")
        if too_short(quote, row["text"]):
            raise GroundingError(f"{name}: the quote is too short to show anything; quote at least a phrase")
        out.append({**item, "source": src})
    return out, ids
