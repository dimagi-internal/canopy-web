"""A knowledge base as content-addressed parts, so publishing sends only what changed.

A bundle is many DOCUMENT RECORDS (each line of items.jsonl: its metadata, text, card
and original) plus a few corpus-wide PARTS (manifest.json, aliases.json, one per
declared table). Each is addressed by the sha256 of its canonical serialisation
(`canonical`: sorted keys, no whitespace, the curator's local `original.path`
dropped), so the same content has the same address on every machine.

A publish is a STATE: `{"docs": {item_id: sha}, "parts": {name: sha}}` (`state`). The
host compares it with the state it serves, asks for the records and parts it lacks,
and applies the difference to its index (`canopy_kb.apply`). An unchanged bundle has
the same state and sends nothing. Passages, sections and the entity index are not
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
    """The item as published: everything but the curator's local path to its original."""
    o = item.get("original")
    if isinstance(o, dict) and "path" in o:
        item = {**item, "original": {k: v for k, v in o.items() if k != "path"}}
    return item


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
            rec = record(it)
            yield iid, sha(rec), rec


def parts(bundle: Path) -> dict[str, object]:
    """The corpus-wide parts of a bundle, by name."""
    b = Path(bundle)
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    out: dict[str, object] = {MANIFEST: manifest}
    a = b / "aliases.json"
    out[ALIASES] = json.loads(a.read_text(encoding="utf-8")) if a.exists() else {}
    for name in manifest.get("tables") or {}:
        p = b / "tables" / f"{name}.jsonl"
        out[TABLE + name] = [json.loads(line) for line in open(p, encoding="utf-8") if line.strip()]
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
