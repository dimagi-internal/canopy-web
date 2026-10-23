"""A knowledge base as content-addressed parts, so publishing sends only what changed.

A bundle is many DOCUMENT RECORDS (each line of items.jsonl: its metadata, card and
original) plus a few corpus-wide PARTS (manifest.json, aliases.json, one per
declared table). Each is addressed by the sha256 of its canonical serialisation
(`canonical`: sorted keys, no whitespace, the curator's local `original.path`
dropped), so the same content has the same address on every machine.

A record carries its document's TEXT by reference (`text_sha`): the text is its own
object, addressed the same way, and `join` puts them back together for the index. A
change to a document's metadata or card then sends a small record, never its text
again; an unchanged text is never sent twice.

A publish is a STATE: `{"docs": {item_id: sha}, "parts": {name: sha}}` (`state`). The
refresh compares it with the state the version it started from published (`diff`), and
applies only the difference to that version's index on its own machine
(`canopy_kb.curate`, `canopy_kb.apply`). An unchanged bundle has the same state: nothing
is built or sent. Passages, sections and the entity index are not
shipped: the index derives them, the same way for a delta as for a full build.

Records travel gzipped (`encode`, with a zero mtime so the bytes are stable too).
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Iterator

#: A part's name: "manifest", "aliases", or "table:<name>".
MANIFEST, ALIASES, TABLE = "manifest", "aliases", "table:"


def record(item: dict) -> dict:
    """The item as published: its text by reference, and nothing of the curator's
    local path to its original."""
    o = item.get("original")
    if isinstance(o, dict) and "path" in o:
        item = {**item, "original": {k: v for k, v in o.items() if k != "path"}}
    if "text" in item:
        item = {k: v for k, v in item.items() if k != "text"} | {"text_sha": sha(item["text"] or "")}
    return item


def join(rec: dict, text: str) -> dict:
    """A record with its text back in place: the item the index stores."""
    return {k: v for k, v in rec.items() if k != "text_sha"} | {"text": text}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def encode(obj) -> bytes:
    return gzip.compress(canonical(obj), compresslevel=6, mtime=0)


def decode(data: bytes, expect_sha: str = ""):
    """A record or part from its stored bytes; refuses bytes that are not `expect_sha`."""
    raw = gzip.decompress(data)
    if expect_sha and hashlib.sha256(raw).hexdigest() != expect_sha:
        raise ValueError(f"stored object is not {expect_sha}")
    return json.loads(raw)


def records(bundle: Path) -> Iterator[tuple[str, str, dict]]:
    """(item id, sha, record) for each item of a bundle, first occurrence of an id
    winning (as `build` does)."""
    for iid, rec, _text in _items(bundle):
        yield iid, sha(rec), rec


def texts(bundle: Path, ids: set[str] | None = None) -> Iterator[tuple[str, str]]:
    """(text sha, text) for the items of a bundle (those in `ids`, when given)."""
    for iid, rec, text in _items(bundle):
        if ids is None or iid in ids:
            yield rec["text_sha"], text


def _items(bundle: Path) -> Iterator[tuple[str, dict, str]]:
    seen: set[str] = set()
    with open(Path(bundle) / "items.jsonl", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            it = json.loads(line)
            iid = it.get("id")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            yield iid, record(it), it.get("text") or ""


def parts(bundle: Path) -> dict[str, object]:
    """The corpus-wide parts of a bundle, by name."""
    b = Path(bundle)
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    out: dict[str, object] = {MANIFEST: manifest}
    a = b / "aliases.json"
    out[ALIASES] = json.loads(a.read_text(encoding="utf-8")) if a.exists() else {}
    for name in manifest.get("tables") or {}:
        p = b / "tables" / f"{name}.jsonl"
        rows = [json.loads(line) for line in open(p, encoding="utf-8") if line.strip()]
        # A part is canonical JSON, whose keys are sorted: the columns' order travels
        # beside the rows, so a table applied from its part reads as the built one.
        out[TABLE + name] = {"columns": list(dict.fromkeys(k for r in rows for k in r)), "rows": rows}
    return out


def tables(parts: dict[str, object]) -> dict[str, list[dict]]:
    """The declared tables' rows (each in its columns' order) from the parts that hold them."""
    out = {}
    for name, part in parts.items():
        if not name.startswith(TABLE):
            continue
        if isinstance(part, dict) and "rows" in part:
            cols = part.get("columns") or []
            out[name[len(TABLE):]] = [{c: r[c] for c in cols if c in r} | r for r in part["rows"]]
        else:  # a part published before the columns travelled with it
            out[name[len(TABLE):]] = list(part or [])
    return out


def state(bundle: Path) -> dict:
    """What a publish of this bundle IS: every record's and part's address."""
    return {"docs": {iid: s for iid, s, _ in records(bundle)},
            "parts": {name: sha(obj) for name, obj in parts(bundle).items()}}


def diff(old: dict, new: dict) -> dict:
    """What turns state `old` into `new`: the docs to add or replace, the ids gone, and
    the parts that changed."""
    od, nd = old.get("docs") or {}, new.get("docs") or {}
    op, np_ = old.get("parts") or {}, new.get("parts") or {}
    return {"added": sorted(i for i in nd if i not in od),
            "changed": sorted(i for i in nd if i in od and od[i] != nd[i]),
            "removed": sorted(i for i in od if i not in nd),
            "parts": sorted(n for n in np_ if op.get(n) != np_[n])}
