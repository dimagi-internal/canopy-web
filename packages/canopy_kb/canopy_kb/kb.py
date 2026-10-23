"""Knowledge bases: a shared, cited library an agent curates and every agent queries.

A knowledge base (KB) is everything one publisher has made public — a funder's grants,
reports, podcast transcripts, cost-effectiveness models — kept as dated copies, with
the tables we compute with alongside. It belongs to a workspace PROJECT, not to any one
agent: the curator agent builds it, and every agent in the workspace reads the same
copy through the same tools.

Two artifacts, one direction:

  bundle/  (what a curator's adapters write — plain files, diffable)
    manifest.json        name, publisher, license, tables{name: {description, date_column,
                         amount_column, filter_columns}}, guide (publisher-specific
                         notes shown by `kb_info`), embed_skip_types (types a vector
                         host would skip; vectors are dormant, see `vectors.py`)
    items.jsonl          one source item per line: id, type, title, url, date, text (markdown),
                         meta, sha256, fetched_at, and optionally card (the catalog card:
                         summary, kind, orgs, programs, countries, period, superseded, answers)
                         and original (the file the text was read from: sha256, filename,
                         content_type, bytes, source_url, fetched_at; `prep/originals.py`)
    tables/<name>.jsonl  structured rows (grants, model parameters, …)
    aliases.json         optional: {kind: [{canonical, names}]} entity names that are one thing

  kb.sqlite (what `build` makes from a bundle and every reader queries)
    items, chunks (passages: spans of the item's text with their bodies) + chunks_fts,
    cards + cards_fts, sections, links, entities + item_entities, one SQL table per
    declared table (plus table_rows), changes, originals, meta

`build` diffs against the previous kb.sqlite by each item's sha256, so `changes`
accumulates a dated history of what appeared, changed or was removed, and it survives
rebuilds because each build carries the old rows forward.

`apply` is the incremental path: a delta (whole new or changed item records, removed
ids, and any changed manifest, aliases or tables) applied to a copy of the previous
kb.sqlite. Both run one writer (`_Index`): per-item rows are put and dropped, the
full-text index is kept in step, and everything derived from more than one item is
recomputed in item-id order — so applying a delta gives the full build of the same
content (tests/test_apply.py). A host serving the KB publishes deltas this way
(`canopy_kb.records`: content-addressed records and states).
`originals` is carried the same way: a file an item no longer points at (the publisher
changed or removed it) stays listed as an earlier version, and its bytes stay in the
host's store, which never deletes one.

Reading is for an agent doing research, not one-shot retrieval (`research.py`): it
orients on the catalog (`overview`, `entity`), searches (words over passages and
cards, ordered by the host's reranker when it has one), then opens documents (`doc`,
`read`, `grep`) and computes over tables (`sql`). Measured on 70 GiveWell questions
with the same agent: 35.5/40 fact questions right against 28.5 with one-shot
passage search over embeddings and words, and 18.5 of 30 research questions won head
to head. No vectors are involved; `vectors.py` keeps the dormant hooks for a host
that wants them.

Every answer carries the item's source URL. A KB holds only what the publisher has
published; relationship notes about the publisher live elsewhere and never merge in.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

from canopy_kb import entities as _entities
from canopy_kb.chunking import chunk, outline
from canopy_kb.vectors import embedding_text, passage_sha

#: 3: passages are spans with a heading path and overlap (an item's text is rebuilt from
#: them, offsets intact); catalog cards, entities, outlines, links and SQL tables for
#: research. Storing the item text as well as the passages doubled the file; slicing
#: passages out of a stored item text made every lookup read whole spreadsheets.
SCHEMA_VERSION = 3
#: Tables of the file itself: a declared table with one of these names is stored as t_<name>.
CORE_TABLES = frozenset({"meta", "items", "chunks", "chunks_fts", "cards", "cards_fts", "sections",
                         "links", "entities", "item_entities", "table_rows", "changes", "originals"})


class KBError(RuntimeError):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(path: Path) -> Iterable[dict]:
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise KBError(f"{path}:{n}: not JSON ({e})") from e


def sql_table_name(name: str) -> str:
    """The SQL table a declared table is stored in."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:60] or "table"
    return f"t_{safe}" if safe in CORE_TABLES or safe[0].isdigit() else safe


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE items (
  id TEXT PRIMARY KEY, type TEXT, title TEXT, url TEXT, date TEXT,
  sha256 TEXT, fetched_at TEXT, first_seen TEXT, meta TEXT, chars INTEGER);
CREATE INDEX items_type ON items(type);
CREATE INDEX items_date ON items(date);
CREATE INDEX items_url ON items(url);
CREATE TABLE chunks (id INTEGER PRIMARY KEY, item_id TEXT, ord INTEGER, start INTEGER, "end" INTEGER,
                     title TEXT, heading TEXT, context TEXT, body TEXT);
CREATE INDEX chunks_item ON chunks(item_id, ord);
CREATE VIRTUAL TABLE chunks_fts USING fts5(title, heading, context, body, content='chunks',
                                           content_rowid='id', tokenize='porter unicode61');
CREATE TABLE cards (item_id TEXT PRIMARY KEY, kind TEXT, summary TEXT, orgs TEXT, programs TEXT,
                    countries TEXT, period TEXT, superseded TEXT, answers TEXT);
CREATE VIRTUAL TABLE cards_fts USING fts5(item_id UNINDEXED, title, summary, answers, entities,
                                          tokenize='porter unicode61');
CREATE TABLE sections (item_id TEXT, ord INTEGER, level INTEGER, path TEXT, start INTEGER, "end" INTEGER);
CREATE INDEX sections_item ON sections(item_id, ord);
CREATE TABLE links (src TEXT, dst TEXT);
CREATE INDEX links_src ON links(src);
CREATE INDEX links_dst ON links(dst);
CREATE TABLE entities (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, aliases TEXT, n_items INTEGER);
CREATE TABLE item_entities (item_id TEXT, entity_id INTEGER);
CREATE INDEX item_entities_item ON item_entities(item_id);
CREATE INDEX item_entities_entity ON item_entities(entity_id);
CREATE TABLE table_rows (tbl TEXT, n INTEGER, data TEXT);
CREATE INDEX table_rows_tbl ON table_rows(tbl);
CREATE TABLE changes (at TEXT, item_id TEXT, change TEXT, type TEXT, title TEXT, url TEXT, date TEXT);
CREATE INDEX changes_at ON changes(at);
CREATE TABLE originals (item_id TEXT, sha256 TEXT, filename TEXT, content_type TEXT, bytes INTEGER,
                        source_url TEXT, fetched_at TEXT, first_seen TEXT, superseded_at TEXT);
CREATE INDEX originals_item ON originals(item_id);
"""

#: An original as stored; `path` (where the curator's copy sits) stays on the curator's machine.
ORIGINAL_FIELDS = ("sha256", "filename", "content_type", "bytes", "source_url", "fetched_at")


def _card(c: Any) -> dict | None:
    if not isinstance(c, dict) or not (c.get("summary") or "").strip():
        return None

    def names(v: Any) -> list[str]:
        return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
    return {"kind": str(c.get("kind") or "").lower().replace("_", " ").strip(), "summary": str(c["summary"]).strip(),
            "orgs": names(c.get("orgs")), "programs": names(c.get("programs")),
            "countries": names(c.get("countries")), "period": str(c.get("period") or ""),
            "superseded": str(c.get("superseded") or ""), "answers": names(c.get("answers"))}


def _load_bundle(bundle: Path) -> tuple[dict, Path, dict, dict[str, list[dict]]]:
    """A bundle's manifest, items path, aliases and declared tables' rows."""
    bundle = Path(bundle)
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        raise KBError(f"{bundle}: no manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items_path = bundle / "items.jsonl"
    if not items_path.exists():
        raise KBError(f"{bundle}: no items.jsonl")
    aliases_path = bundle / "aliases.json"
    aliases = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.exists() else {}
    tables: dict[str, list[dict]] = {}
    for name in (manifest.get("tables") or {}):
        p = bundle / "tables" / f"{name}.jsonl"
        if not p.exists():
            raise KBError(f"manifest declares table {name!r} but {p} is missing")
        tables[name] = list(_read_jsonl(p))
    return manifest, items_path, aliases, tables


class _Index:
    """The one writer of a kb.sqlite's rows, shared by `build` (every item, into an empty
    file) and `apply` (a delta, into a copy of the previous file). Because both run the
    same code, an incrementally maintained index is the full build of the same content:
    the per-item rows are written by `put` and removed by `drop`, and everything that
    depends on more than one item (links, the entity index, card search, tables, the
    summary) is recomputed from the stored rows by `finish`, in item-id order."""

    def __init__(self, db: sqlite3.Connection, now: str, live_fts: bool):
        self.db, self.now = db, now
        # build bulk-indexes passages once at the end ('rebuild'); apply keeps the
        # external-content FTS table in step row by row ('delete' needs the old values).
        self.live_fts = live_fts

    def put(self, it: dict, first_seen: str) -> None:
        db, iid = self.db, it["id"]
        text = it.get("text") or ""
        meta = it.get("meta") or {}
        title = it.get("title", "")
        db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", (
            iid, it.get("type", ""), title, it.get("url", ""), it.get("date", "") or "",
            it.get("sha256") or "", it.get("fetched_at", ""), first_seen, json.dumps(meta), len(text)))
        db.executemany('INSERT INTO chunks(item_id, ord, start, "end", title, heading, context, body) '
                       'VALUES (?,?,?,?,?,?,?,?)',
                       [(iid, i, p.start, p.end, title, p.heading, "", text[p.start:p.end])
                        for i, p in enumerate(chunk(text))])
        if self.live_fts:
            db.execute("INSERT INTO chunks_fts(rowid, title, heading, context, body) "
                       "SELECT id, title, heading, context, body FROM chunks WHERE item_id = ?", (iid,))
        db.executemany('INSERT INTO sections VALUES (?,?,?,?,?,?)',
                       [(iid, i, s.level, s.path, s.start, s.end) for i, s in enumerate(outline(text))])
        c = _card(it.get("card"))
        if c:
            db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)", (
                iid, c["kind"], c["summary"], json.dumps(c["orgs"]), json.dumps(c["programs"]),
                json.dumps(c["countries"]), c["period"], c["superseded"], json.dumps(c["answers"])))
        o = it.get("original")
        self._original(iid, o if isinstance(o, dict) and o.get("sha256") else None)

    def _original(self, iid: str, o: dict | None) -> None:
        """The item's current original is `o`; any other it pointed at is now an earlier
        version, dated when it was first found superseded. Never deleted."""
        db, sha = self.db, (o or {}).get("sha256") or ""
        db.execute("UPDATE originals SET superseded_at = ? WHERE item_id = ? AND superseded_at = '' AND sha256 != ?",
                   (self.now, iid, sha))
        if not o:
            return
        fields = (o.get("filename") or "", o.get("content_type") or "", int(o.get("bytes") or 0),
                  o.get("source_url") or "", o.get("fetched_at") or "")
        if db.execute("SELECT 1 FROM originals WHERE item_id = ? AND sha256 = ?", (iid, sha)).fetchone():
            db.execute("UPDATE originals SET filename = ?, content_type = ?, bytes = ?, source_url = ?, fetched_at = ?, "
                       "superseded_at = '' WHERE item_id = ? AND sha256 = ?", (*fields, iid, sha))
        else:
            db.execute("INSERT INTO originals VALUES (?,?,?,?,?,?,?,?,?)", (iid, sha, *fields, self.now, ""))

    def drop(self, iid: str, removed: bool) -> None:
        """Delete an item's own rows. Its originals stay: a changed item's `put` decides
        which is current, and a removed item's become earlier versions."""
        db = self.db
        if self.live_fts:
            db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, title, heading, context, body) "
                       "SELECT 'delete', id, title, heading, context, body FROM chunks WHERE item_id = ?", (iid,))
        for table, col in (("items", "id"), ("chunks", "item_id"), ("sections", "item_id"), ("cards", "item_id")):
            db.execute(f"DELETE FROM {table} WHERE {col} = ?", (iid,))
        if removed:
            self._original(iid, None)

    def change(self, iid: str, change: str, it: dict | sqlite3.Row) -> None:
        self.db.execute("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", (
            self.now, iid, change, it["type"], it["title"], it["url"], it["date"]))

    def finish(self, manifest: dict, aliases: dict, tables: dict[str, list[dict]] | None,
               old_manifest: dict | None, counts: dict) -> dict:
        """Everything derived from more than one item, recomputed from the stored rows.
        `tables` holds the declared tables to (re)write; one left out keeps its rows."""
        db = self.db
        rows = db.execute("SELECT id, type, title, url, meta FROM items ORDER BY id").fetchall()
        ids = {r[0] for r in rows}
        refs: dict[str, str] = {}        # url (and also_at urls) -> item id, for resolving links
        raw_links: list[tuple[str, str]] = []
        titles: dict[str, str] = {}
        for iid, _type, title, url, meta_json in rows:
            meta = json.loads(meta_json or "{}")
            titles[iid] = title or ""
            for u in [url or ""] + list(meta.get("also_at") or []):
                if u:
                    refs.setdefault(u.rstrip("/"), iid)
            raw_links += [(str(src), iid) for src in meta.get("linked_from") or []]
            if meta.get("parent"):
                raw_links.append((str(meta["parent"]), iid))
        # links: a curator names an item's referrers by id or url
        pairs = set()
        for src, dst in raw_links:
            s = src if src in ids else refs.get(src.rstrip("/"))
            if s and s != dst:
                pairs.add((s, dst))
        db.execute("DELETE FROM links")
        db.executemany("INSERT INTO links VALUES (?,?)", sorted(pairs))

        # catalog cards and the entity index built from them
        cards: dict[str, dict] = {}
        for r in db.execute("SELECT item_id, summary, answers, orgs, programs, countries FROM cards ORDER BY item_id"):
            cards[r[0]] = {"summary": r[1], "answers": json.loads(r[2] or "[]"), "orgs": json.loads(r[3] or "[]"),
                           "programs": json.loads(r[4] or "[]"), "countries": json.loads(r[5] or "[]")}
        ents, by_item = _entities.index(cards, aliases)
        for t in ("entities", "item_entities", "cards_fts"):
            db.execute(f"DELETE FROM {t}")
        db.executemany("INSERT INTO entities VALUES (?,?,?,?,?)",
                       [(i, e["kind"], e["name"], json.dumps(e["aliases"]), len(e["items"])) for i, e in enumerate(ents)])
        db.executemany("INSERT INTO item_entities VALUES (?,?)",
                       [(iid, e) for iid in sorted(by_item) for e in sorted(by_item[iid])])
        for iid, c in cards.items():
            names = " ; ".join(a for e in sorted(by_item.get(iid, ())) for a in ents[e]["aliases"])
            db.execute("INSERT INTO cards_fts VALUES (?,?,?,?,?)",
                       (iid, titles.get(iid, ""), c["summary"], " ".join(c["answers"]), names))

        declared = manifest.get("tables") or {}
        for name in set((old_manifest or {}).get("tables") or {}) | set(tables or {}):
            if name not in declared or name in (tables or {}):
                db.execute("DELETE FROM table_rows WHERE tbl = ?", (name,))
                db.execute(f'DROP TABLE IF EXISTS "{sql_table_name(name)}"')
        for name, trows in (tables or {}).items():
            if name not in declared:
                continue
            db.executemany("INSERT INTO table_rows VALUES (?,?,?)", [(name, i, json.dumps(r)) for i, r in enumerate(trows)])
            cols = list(dict.fromkeys(k for r in trows for k in r))
            if cols:
                tn = sql_table_name(name)
                db.execute(f'CREATE TABLE "{tn}" ({", ".join(chr(34) + c.replace(chr(34), "") + chr(34) for c in cols)})')
                db.executemany(f'INSERT INTO "{tn}" VALUES ({",".join("?" * len(cols))})',
                               [[v if v is None or isinstance(v, (int, float, str)) else json.dumps(v)
                                 for v in (r.get(c) for c in cols)] for r in trows])
        n_tables = {name: db.execute("SELECT count(*) FROM table_rows WHERE tbl = ?", (name,)).fetchone()[0]
                    for name in declared}

        current = db.execute("SELECT count(*), count(DISTINCT sha256) FROM originals WHERE superseded_at = ''").fetchone()
        n_bytes = db.execute("SELECT coalesce(sum(b), 0) FROM (SELECT max(bytes) b FROM originals "
                             "WHERE superseded_at = '' GROUP BY sha256)").fetchone()[0]
        n_superseded = db.execute("SELECT count(*) FROM originals WHERE superseded_at != ''").fetchone()[0]
        summary = {"schema": SCHEMA_VERSION, "built_at": self.now, "name": manifest.get("name", ""),
                   "items": len(ids), "passages": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                   "cards": len(cards), "entities": len(ents), "links": len(pairs),
                   "by_type": dict(db.execute("SELECT type, count(*) FROM items GROUP BY type ORDER BY type").fetchall()),
                   "tables": n_tables,
                   "originals": {"current": current[0], "superseded": n_superseded, "files": current[1], "bytes": n_bytes},
                   **counts}
        db.execute("DELETE FROM meta")
        db.executemany("INSERT INTO meta VALUES (?,?)", [
            ("manifest", json.dumps(manifest)), ("aliases", json.dumps(aliases)), ("summary", json.dumps(summary)),
            ("schema", str(SCHEMA_VERSION))])
        return summary


def build(bundle: Path, out: Path, previous: Path | None = None) -> dict:
    """Build kb.sqlite from a bundle directory. Returns a summary dict."""
    manifest, items_path, aliases, tables = _load_bundle(bundle)
    prev: dict[str, tuple[str, str]] = {}
    prev_items: dict[str, sqlite3.Row] = {}
    prev_changes: list[tuple] = []
    prev_originals: list[dict] = []
    if previous and Path(previous).is_file() and Path(previous).stat().st_size:
        pc = sqlite3.connect(f"file:{previous}?mode=ro", uri=True)
        pc.row_factory = sqlite3.Row
        try:
            for r in pc.execute("SELECT id, type, title, url, date, sha256, first_seen FROM items"):
                prev[r["id"]] = (r["sha256"], r["first_seen"])
                prev_items[r["id"]] = r
            prev_changes = [tuple(r) for r in pc.execute("SELECT at, item_id, change, type, title, url, date FROM changes")]
            if pc.execute("SELECT 1 FROM sqlite_master WHERE name = 'originals'").fetchone():
                prev_originals = [dict(r) for r in pc.execute("SELECT * FROM originals")]
        except sqlite3.DatabaseError as e:
            raise KBError(f"--previous {previous} is not a knowledge base ({e})") from e
        finally:
            pc.close()
    else:
        previous = None

    tmp = Path(str(out) + ".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    now = _now()
    db.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", prev_changes)
    db.executemany("INSERT INTO originals VALUES (?,?,?,?,?,?,?,?,?)", [
        (r["item_id"], r["sha256"], r["filename"], r["content_type"], r["bytes"], r["source_url"],
         r["fetched_at"], r["first_seen"], r["superseded_at"] or "") for r in prev_originals])
    idx = _Index(db, now, live_fts=False)
    new = changed = 0
    seen: set[str] = set()
    for it in _read_jsonl(items_path):
        iid = it.get("id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        old = prev.get(iid)
        if previous and not old:
            new += 1
            idx.change(iid, "new", {k: it.get(k) for k in ("type", "title", "url", "date")})
        elif old and it.get("sha256") and old[0] != it.get("sha256"):
            changed += 1
            idx.change(iid, "changed", {k: it.get(k) for k in ("type", "title", "url", "date")})
        idx.put(it, old[1] if old else now)
    removed = sorted(set(prev) - seen)
    for iid in removed:
        idx.change(iid, "removed", prev_items[iid])
        idx._original(iid, None)
    summary = idx.finish(manifest, aliases, tables, None, {
        "new": new, "changed": changed, "removed": len(removed), "baseline": not bool(previous and prev)})
    db.commit()
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
    db.execute("INSERT INTO cards_fts(cards_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(out)
    return summary


def apply(previous: Path | None, out: Path, upserts: Iterable[dict], removed: Iterable[str] = (), *,
          manifest: dict | None = None, aliases: dict | None = None,
          tables: dict[str, list[dict]] | None = None) -> dict:
    """Apply a delta to a previous kb.sqlite, writing a new one at `out` (the previous
    file is only read). `upserts` are whole item records (new or changed), `removed` the
    ids that are gone; `manifest` and `aliases` replace the previous ones when given, and
    `tables` holds only the declared tables whose rows changed. The result is what `build`
    makes of the same content (`_Index`). Returns the summary, with `applied`: the ids
    this delta added, changed and removed. `previous=None` is a knowledge base's first
    version: every record is new, into an empty file."""
    out = Path(out)
    tmp = Path(str(out) + ".apply")
    part = Path(str(out) + ".part")
    for p in (tmp, part):
        p.unlink(missing_ok=True)
    if previous is None:
        db = sqlite3.connect(tmp)
        db.executescript(SCHEMA)
    else:
        previous = Path(previous)
        shutil.copyfile(previous, tmp)
        db = sqlite3.connect(tmp)
    db.row_factory = sqlite3.Row
    try:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        if previous is not None and int(json.loads(meta.get("summary") or "{}").get("schema") or 0) < SCHEMA_VERSION:
            raise KBError(f"{previous} is an older schema; publish a full build first")
        old_manifest = json.loads(meta.get("manifest") or "{}")
        if aliases is None:
            if previous is None:
                aliases = {}
            elif "aliases" not in meta:
                raise KBError(f"{previous} does not carry its aliases; pass them")
            else:
                aliases = json.loads(meta["aliases"])
        idx = _Index(db, _now(), live_fts=True)
        # `changed`: every document re-put; `content_changed`: those whose text changed
        applied: dict[str, list[str]] = {"new": [], "changed": [], "content_changed": [], "removed": []}
        seen: set[str] = set()
        for it in upserts:
            iid = it.get("id")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            old = db.execute("SELECT sha256, first_seen FROM items WHERE id = ?", (iid,)).fetchone()
            row = {k: it.get(k) for k in ("type", "title", "url", "date")}
            if old is None:
                applied["new"].append(iid)
                if previous is not None:  # a first version is a baseline, as `build` makes it
                    idx.change(iid, "new", row)
            else:
                applied["changed"].append(iid)
                if it.get("sha256") and old["sha256"] != it.get("sha256"):
                    applied["content_changed"].append(iid)
                    idx.change(iid, "changed", row)
                idx.drop(iid, removed=False)
            idx.put(it, old["first_seen"] if old else idx.now)
        for iid in removed:
            if iid in seen:
                continue
            old = db.execute("SELECT type, title, url, date FROM items WHERE id = ?", (iid,)).fetchone()
            if old is None:
                continue
            applied["removed"].append(iid)
            idx.change(iid, "removed", old)
            idx.drop(iid, removed=True)
        summary = idx.finish(manifest if manifest is not None else old_manifest, aliases, tables or {}, old_manifest, {
            "new": len(applied["new"]) if previous is not None else 0, "changed": len(applied["changed"]),
            "removed": len(applied["removed"]), "baseline": previous is None})
        db.commit()
        db.execute("VACUUM INTO ?", (str(part),))
    finally:
        db.close()
        tmp.unlink(missing_ok=True)
    part.replace(out)
    return {**summary, "applied": applied}


class KB:
    """Read side. The passage methods return plain dicts with the source URL on each
    row; the research methods (`research.py`) return text an agent reads."""

    def __init__(self, path: Path):
        path = Path(path)
        if not path.exists():
            raise KBError(f"{path}: no such knowledge base (run `canopy kb pull` first)")
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        m = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        self.manifest = json.loads(m.get("manifest", "{}"))
        self.summary = json.loads(m.get("summary", "{}"))
        if int(self.summary.get("schema") or 0) < SCHEMA_VERSION:
            raise KBError(f"{path} is a schema {self.summary.get('schema')} knowledge base; "
                          f"rebuild it with this version (schema {SCHEMA_VERSION})")

    # -- discovery -------------------------------------------------------------------
    def info(self) -> dict:
        return {"manifest": self.manifest, "summary": self.summary}

    # -- research (text for an agent; see research.py) -----------------------------
    def overview(self) -> str:
        from canopy_kb import research
        return research.overview(self)

    def entity(self, name: str, type_: str = "") -> str:
        from canopy_kb import research
        return research.entity(self, name, type_)

    def find(self, query: str, **kw) -> str:
        from canopy_kb import research
        return research.search(self, query, **kw)

    def doc(self, ref: str) -> str:
        from canopy_kb import research
        return research.doc(self, ref)

    def read(self, ref: str, section: int | None = None, at: int = 0, chars: int = 15000) -> str:
        from canopy_kb import research
        return research.read(self, ref, section, at, chars)

    def grep(self, pattern: str, ref: str = "", entity: str = "", limit: int = 40) -> str:
        from canopy_kb import research
        return research.grep(self, pattern, ref, entity, limit)

    def sql(self, query: str, limit: int = 200) -> str:
        from canopy_kb import research
        return research.sql(self, query, limit)

    # -- passages ------------------------------------------------------------------
    def passage_text(self, item_id: str, ord_: int) -> str:
        r = self.db.execute("SELECT body FROM chunks WHERE item_id = ? AND ord = ?", (item_id, ord_)).fetchone()
        return (r[0] if r else "") or ""

    def text(self, item_id: str, start: int = 0, end: int | None = None) -> str:
        """The item's text, or the slice [start, end), rebuilt from its passages: an
        overlap is taken once and a gap between passages (only ever whitespace) is a
        newline per character, so offsets are the source's."""
        hi = end if end is not None else 1 << 62
        out, pos = [], start
        for s, e, body in self.db.execute('SELECT start, "end", body FROM chunks WHERE item_id = ? AND "end" > ? '
                                          'AND start < ? ORDER BY ord', (item_id, start, hi)):
            if e <= pos:
                continue
            if s > pos:
                out.append("\n" * (s - pos))
                pos = s
            stop = min(e, hi)
            out.append(body[pos - s: stop - s])
            pos = stop
        return "".join(out)

    def passages(self) -> Iterator[dict]:
        """Every passage with what its vector would be made of (`vectors.embedding_text`)
        and keyed by (`vectors.passage_sha`), in file order: a vector host's sync reads
        this. Dormant — no host keeps vectors today.

        The manifest's `embed_skip_types` (e.g. raw data tabs) are left out: they stay
        searchable by words and readable, and are not worth a vector each."""
        skip = set(self.manifest.get("embed_skip_types") or [])
        for r in self.db.execute(
                "SELECT c.item_id, c.ord, c.heading, c.context, c.body, i.type, i.title, i.url, i.date FROM chunks c "
                "JOIN items i ON i.id = c.item_id ORDER BY c.id"):
            if r["type"] in skip:
                continue
            text = embedding_text(r["title"] or "", r["type"] or "", r["date"] or "", r["body"] or "",
                                  r["heading"] or "", r["context"] or "")
            if not text:
                continue
            yield {"item_id": r["item_id"], "ord": r["ord"], "type": r["type"] or "", "title": r["title"] or "",
                   "url": r["url"] or "", "date": r["date"] or "", "text": text, "sha256": passage_sha(text)}

    # -- originals -------------------------------------------------------------------
    def originals(self, item_id: str) -> list[dict]:
        """The item's original files: the current one first, then earlier versions, newest
        first. Empty for a KB built before originals were kept."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'originals'").fetchone():
            return []
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM originals WHERE item_id = ? ORDER BY superseded_at != '', first_seen DESC, rowid DESC",
            (item_id,))]

    def has_originals(self) -> bool:
        try:
            return bool(self.db.execute("SELECT 1 FROM originals LIMIT 1").fetchone())
        except sqlite3.OperationalError:
            return False

    # -- items -----------------------------------------------------------------------
    def item_row(self, ref: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM items WHERE id = ? OR url = ? LIMIT 1", (ref, ref)).fetchone()

    def get(self, ref: str, max_chars: int = 20000, passage: int | None = None,
            grep: str = "", context: int = 1) -> dict:
        r = self.item_row(ref)
        if not r:
            raise KBError(f"no item {ref!r}")
        d = dict(r)
        d["meta"] = json.loads(d["meta"] or "{}")
        d["text"] = self.passage_text(d["id"], passage) if passage is not None else self.text(d["id"])
        d["passages"] = self.db.execute("SELECT COUNT(*) FROM chunks WHERE item_id = ?", (d["id"],)).fetchone()[0]
        if grep:  # verify a quote or a number without pulling the whole item into context
            try:
                rx = re.compile(grep, re.I)
            except re.error as e:
                raise KBError(f"bad grep pattern: {e}") from e
            lines = (d["text"] or "").splitlines()
            hits = [i for i, line in enumerate(lines) if rx.search(line)]
            d["matches"] = [{"line": i + 1, "text": "\n".join(lines[max(0, i - context): i + context + 1])}
                            for i in hits[:50]]
            d["match_count"] = len(hits)
            del d["text"]
            return d
        full = len(d["text"] or "")
        if max_chars and full > max_chars:
            d["text"] = d["text"][:max_chars]
            d["truncated"] = f"{max_chars} of {full} chars; pass a larger max_chars or a passage number"
        return d

    def list_items(self, types: list[str] | None = None, since: str = "", limit: int = 50) -> list[dict]:
        sql, args = "SELECT id, type, title, url, date FROM items WHERE 1=1", []
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        if since:
            sql += " AND date >= ?"
            args.append(since)
        sql += " ORDER BY date DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    # -- tables ----------------------------------------------------------------------
    def tables(self) -> dict:
        return {name: {**spec, "rows": self.summary.get("tables", {}).get(name), "sql_table": sql_table_name(name)}
                for name, spec in (self.manifest.get("tables") or {}).items()}

    def table(self, name: str, match: dict[str, str] | None = None, text: str = "", since: str = "",
              until: str = "", min_amount: float | None = None, sort: str = "", limit: int = 50) -> dict:
        spec = (self.manifest.get("tables") or {}).get(name)
        if spec is None:
            raise KBError(f"no table {name!r}; have {sorted(self.manifest.get('tables') or {})}")
        date_col, amt_col = spec.get("date_column"), spec.get("amount_column")
        rows = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM table_rows WHERE tbl = ? ORDER BY n", (name,))]

        def ok(row: dict) -> bool:
            for col, needle in (match or {}).items():
                if needle.lower() not in str(row.get(col, "")).lower():
                    return False
            if text and text.lower() not in json.dumps(row, ensure_ascii=False).lower():
                return False
            d = str(row.get(date_col, ""))[:10] if date_col else ""
            if since and (not d or d < since):
                return False
            if until and (not d or d > until):
                return False
            if min_amount is not None and amt_col:
                try:
                    if float(row.get(amt_col) or 0) < min_amount:
                        return False
                except (TypeError, ValueError):
                    return False
            return True

        hits = [r for r in rows if ok(r)]
        if sort:
            col, rev = sort.lstrip("-"), sort.startswith("-")

            def key(r):
                v = r.get(col)
                return (0, v) if isinstance(v, (int, float)) else (1, str(v or ""))
            hits.sort(key=key, reverse=rev)
        total = None
        if amt_col:
            try:
                total = round(sum(float(r.get(amt_col) or 0) for r in hits), 2)
            except (TypeError, ValueError):
                total = None
        return {"table": name, "matched": len(hits), "total_amount": total, "rows": hits[:limit],
                "truncated": len(hits) > limit}

    # -- change history --------------------------------------------------------------
    def whats_new(self, since: str = "", limit: int = 100) -> dict:
        """The file's own change history (new, changed, removed per build or delta). A host
        that keeps a version log answers from that instead (canopy-web does)."""
        sql, args = "SELECT * FROM changes", []
        if since:
            sql += " WHERE at >= ?"
            args.append(since)
        sql += " ORDER BY at DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        note = ""
        if self.summary.get("baseline") and not rows:
            note = (f"baseline build at {self.summary.get('built_at')}: everything is new; "
                    "change history starts with the next refresh")
        return {"built_at": self.summary.get("built_at"), "changes": rows, "note": note}
