"""Knowledge bases: a shared, cited library an agent curates and every agent queries.

A knowledge base (KB) is everything one publisher has made public — a funder's grants,
reports, podcast transcripts, cost-effectiveness models — kept as dated copies, with
the tables we compute with alongside. It belongs to a workspace PROJECT, not to any one
agent: the curator agent builds it, and every agent in the workspace reads the same
copy through the same tools.

Two artifacts, one direction:

  bundle/  (what a curator's adapters write — plain files, diffable)
    manifest.json        name, publisher, license, tables{name: {description, date_column,
                         amount_column, filter_columns}}, search_boost{type: factor}
                         (rank the publisher's own analysis above documents it merely hosts),
                         guide (publisher-specific notes shown by `kb_info`)
    items.jsonl          one source item per line: id, type, title, url, date, text (markdown),
                         meta, sha256, fetched_at, and optionally card (the catalog card:
                         summary, kind, orgs, programs, countries, period, superseded, answers)
    tables/<name>.jsonl  structured rows (grants, model parameters, …)
    aliases.json         optional: {kind: [{canonical, names}]} entity names that are one thing

  kb.sqlite (what `build` makes from a bundle and every reader queries)
    items, chunks (passages: spans of the item's text with their bodies) + chunks_fts,
    cards + cards_fts, sections, links, entities + item_entities, one SQL table per
    declared table (plus table_rows), changes, meta

`build` diffs against the previous kb.sqlite by each item's sha256, so `changes`
accumulates a dated history of what appeared or changed — that is what `whats_new`
answers, and it survives rebuilds because each build carries the old rows forward.

Reading is for an agent doing research, not one-shot retrieval (`research.py`): it
orients on the catalog (`overview`, `entity`), searches (words over passages and
cards, reranked by meaning when the host has a reranker, fused with the host's
nearest passages when it keeps vectors), then opens documents (`doc`, `read`,
`grep`) and computes over tables (`sql`). Measured on 70 GiveWell questions with
the same agent: 33-34/40 fact questions right against 27-28 with passage search
alone, and 20.5 of 30 research questions won head to head.

Every answer carries the item's source URL. A KB holds only what the publisher has
published; relationship notes about the publisher live elsewhere and never merge in.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from canopy_kb import entities as _entities
from canopy_kb.chunking import chunk, outline
from canopy_kb.hybrid import (CANDIDATES, DENSE_WEIGHT, MODES, RERANK_DEPTH, embedding_text, passage_sha,
                              rrf)

if TYPE_CHECKING:
    from canopy_kb.hybrid import Rerank, Semantic

#: English function words dropped from a search query (not from the index).
STOPWORDS = frozenset("""a about above after again against all am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from further had has
have having he her here hers herself him himself his how i if in into is it its itself just me more most my
myself no nor not now of off on once only or other our ours ourselves out over own same she should so some
such than that the their theirs them themselves then there these they this those through to too under until
up very was we were what when where which while who whom why will with would you your yours yourself
yourselves""".split())
#: A semantic hit has no matched words to show around, so its row shows the
#: start of the passage instead.
SNIPPET_CHARS = 300
#: 3: passages are spans with a heading path and overlap (an item's text is rebuilt from
#: them, offsets intact); catalog cards, entities, outlines, links and SQL tables for
#: research. Storing the item text as well as the passages doubled the file; slicing
#: passages out of a stored item text made every lookup read whole spreadsheets.
SCHEMA_VERSION = 3
#: Tables of the file itself: a declared table with one of these names is stored as t_<name>.
CORE_TABLES = frozenset({"meta", "items", "chunks", "chunks_fts", "cards", "cards_fts", "sections",
                         "links", "entities", "item_entities", "table_rows", "changes"})


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
"""


def _card(c: Any) -> dict | None:
    if not isinstance(c, dict) or not (c.get("summary") or "").strip():
        return None

    def names(v: Any) -> list[str]:
        return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
    return {"kind": str(c.get("kind") or "").lower().replace("_", " ").strip(), "summary": str(c["summary"]).strip(),
            "orgs": names(c.get("orgs")), "programs": names(c.get("programs")),
            "countries": names(c.get("countries")), "period": str(c.get("period") or ""),
            "superseded": str(c.get("superseded") or ""), "answers": names(c.get("answers"))}


def build(bundle: Path, out: Path, previous: Path | None = None) -> dict:
    """Build kb.sqlite from a bundle directory. Returns a summary dict."""
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

    prev: dict[str, tuple[str, str]] = {}
    prev_changes: list[tuple] = []
    if previous and Path(previous).is_file() and Path(previous).stat().st_size:
        pc = sqlite3.connect(f"file:{previous}?mode=ro", uri=True)
        try:
            prev = {r[0]: (r[1], r[2]) for r in pc.execute("SELECT id, sha256, first_seen FROM items")}
            prev_changes = list(pc.execute("SELECT at, item_id, change, type, title, url, date FROM changes"))
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
    counts: dict[str, int] = {}
    new = changed = 0
    seen: set[str] = set()
    cards: dict[str, dict] = {}
    titles: dict[str, str] = {}
    refs: dict[str, str] = {}        # url (and also_at urls) -> item id, for resolving links
    raw_links: list[tuple[str, str]] = []
    for it in _read_jsonl(items_path):
        iid = it.get("id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        text = it.get("text") or ""
        sha = it.get("sha256") or ""
        meta = it.get("meta") or {}
        old = prev.get(iid)
        first_seen = old[1] if old else now
        if previous and not old:
            new += 1
            prev_changes.append((now, iid, "new", it.get("type"), it.get("title"), it.get("url"), it.get("date")))
        elif old and sha and old[0] != sha:
            changed += 1
            prev_changes.append((now, iid, "changed", it.get("type"), it.get("title"), it.get("url"), it.get("date")))
        db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", (
            iid, it.get("type", ""), it.get("title", ""), it.get("url", ""), it.get("date", "") or "",
            sha, it.get("fetched_at", ""), first_seen, json.dumps(meta), len(text)))
        title = it.get("title", "")
        db.executemany('INSERT INTO chunks(item_id, ord, start, "end", title, heading, context, body) '
                       'VALUES (?,?,?,?,?,?,?,?)',
                       [(iid, i, p.start, p.end, title, p.heading, "", text[p.start:p.end])
                        for i, p in enumerate(chunk(text))])
        db.executemany('INSERT INTO sections VALUES (?,?,?,?,?,?)',
                       [(iid, i, s.level, s.path, s.start, s.end) for i, s in enumerate(outline(text))])
        card = _card(it.get("card"))
        if card:
            cards[iid] = card
        titles[iid] = it.get("title", "")
        for u in [it.get("url", "")] + list(meta.get("also_at") or []):
            if u:
                refs.setdefault(u.rstrip("/"), iid)
        for src in meta.get("linked_from") or []:
            raw_links.append((str(src), iid))
        if meta.get("parent"):
            raw_links.append((str(meta["parent"]), iid))
        counts[it.get("type", "")] = counts.get(it.get("type", ""), 0) + 1

    # links: a curator names an item's referrers by id or url
    pairs = set()
    for src, dst in raw_links:
        s = src if src in seen else refs.get(src.rstrip("/"))
        if s and s != dst:
            pairs.add((s, dst))
    db.executemany("INSERT INTO links VALUES (?,?)", sorted(pairs))

    # catalog cards and the entity index built from them
    ents, by_item = _entities.index(cards, aliases)
    db.executemany("INSERT INTO entities VALUES (?,?,?,?,?)",
                   [(i, e["kind"], e["name"], json.dumps(e["aliases"]), len(e["items"])) for i, e in enumerate(ents)])
    db.executemany("INSERT INTO item_entities VALUES (?,?)", [(iid, e) for iid, es in by_item.items() for e in sorted(es)])
    for iid, c in cards.items():
        db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)", (
            iid, c["kind"], c["summary"], json.dumps(c["orgs"]), json.dumps(c["programs"]),
            json.dumps(c["countries"]), c["period"], c["superseded"], json.dumps(c["answers"])))
        names = " ; ".join(a for e in sorted(by_item.get(iid, ())) for a in ents[e]["aliases"])
        db.execute("INSERT INTO cards_fts VALUES (?,?,?,?,?)",
                   (iid, titles.get(iid, ""), c["summary"], " ".join(c["answers"]), names))

    tables: dict[str, int] = {}
    tdir = bundle / "tables"
    for name in (manifest.get("tables") or {}):
        p = tdir / f"{name}.jsonl"
        if not p.exists():
            raise KBError(f"manifest declares table {name!r} but {p} is missing")
        rows = list(_read_jsonl(p))
        db.executemany("INSERT INTO table_rows VALUES (?,?,?)", [(name, i, json.dumps(r)) for i, r in enumerate(rows)])
        cols = list(dict.fromkeys(k for r in rows for k in r))
        if cols:
            tn = sql_table_name(name)
            db.execute(f'CREATE TABLE "{tn}" ({", ".join(chr(34) + c.replace(chr(34), "") + chr(34) for c in cols)})')
            db.executemany(f'INSERT INTO "{tn}" VALUES ({",".join("?" * len(cols))})',
                           [[v if v is None or isinstance(v, (int, float, str)) else json.dumps(v)
                             for v in (r.get(c) for c in cols)] for r in rows])
        tables[name] = len(rows)

    db.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", prev_changes)
    n_passages = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    summary = {"schema": SCHEMA_VERSION, "built_at": now, "name": manifest.get("name", ""),
               "items": len(seen), "passages": n_passages, "cards": len(cards), "entities": len(ents),
               "links": len(pairs), "by_type": counts, "tables": tables,
               "new": new, "changed": changed, "baseline": not bool(previous and prev)}
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("manifest", json.dumps(manifest)), ("summary", json.dumps(summary)), ("schema", str(SCHEMA_VERSION))])
    db.commit()
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
    db.execute("INSERT INTO cards_fts(cards_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(out)
    return summary


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

    # -- passage search (one row per item; kb_eval and the vector sync use this) ---------
    @staticmethod
    def _fts_query(q: str, mode: str) -> str:
        phrases = re.findall(r'"([^"]+)"', q)
        rest = re.sub(r'"[^"]+"', " ", q)
        terms = [t for t in re.findall(r"[\w$%.-]+", rest) if len(t) > 1 or t.isdigit()]
        # Words that carry no topic: a question's "what are the …" would otherwise make
        # the OR fallback match nearly every passage. Kept if the query is nothing else.
        content = [t for t in terms if t.lower().strip(".-") not in STOPWORDS]
        terms = content or terms
        parts = ['"' + p.replace('"', "") + '"' for p in phrases]
        parts += ['"' + t.strip(".-") + '"' for t in terms if t.strip(".-")]
        return f" {mode} ".join(parts)

    def search(self, query: str, types: list[str] | None = None, since: str = "",
               until: str = "", limit: int = 10, mode: str = "hybrid",
               semantic: "Semantic | None" = None, rerank: "Rerank | None" = None,
               dense_weight: float = DENSE_WEIGHT) -> list[dict]:
        """Best passages for a question, one per item.

        `lexical` is BM25 over the FTS index; `semantic` is the host's nearest
        passages by meaning (`semantic`, see `hybrid.py`); `hybrid` fuses the two
        by reciprocal rank, the meaning list weighted `dense_weight` and the words
        list 1 - `dense_weight`. Without a `semantic` callable, or when it has no
        vectors to ask (returns None), every mode is lexical. A `rerank` callable
        then rescores the top RERANK_DEPTH against the question; when it cannot,
        the order stands."""
        if not query.strip():
            raise KBError("empty query")
        if mode not in MODES:
            raise KBError(f"mode must be one of {', '.join(MODES)}")
        depth = max(limit, CANDIDATES, RERANK_DEPTH if rerank else 0)
        ranked = self._ranked(query, types, since, until, depth, mode, semantic, dense_weight)
        if rerank is not None and len(ranked) > 1:
            ranked = self._reranked(query, ranked[:RERANK_DEPTH], rerank)
        return ranked[:limit]

    def _ranked(self, query, types, since, until, depth, mode, semantic, dense_weight) -> list[dict]:
        if mode == "lexical" or semantic is None:
            return self._lexical(query, types, since, until, depth)
        near = semantic(query, types=types or None, since=since, until=until, limit=depth)
        if near is None:
            return self._lexical(query, types, since, until, depth)
        by_meaning = self._passage_rows(near)
        if mode == "semantic":
            return by_meaning
        by_words = self._lexical(query, types, since, until, depth)
        rows = {r["id"]: r for r in by_meaning}
        rows.update({r["id"]: r for r in by_words})  # BM25's snippet marks the matched words
        fused = rrf([[r["id"] for r in by_words], [r["id"] for r in by_meaning]],
                    weights=[1.0 - dense_weight, dense_weight])
        return [rows[i] for i in fused]

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

    def _reranked(self, query: str, rows: list[dict], rerank: "Rerank") -> list[dict]:
        docs = [{"title": r["title"] or "", "text": self.passage_text(r["id"], r["passage"]) or r["snippet"] or ""}
                for r in rows]
        order = list(rerank(query, docs) or [])
        if not order or len(set(order)) != len(order) or any(not 0 <= i < len(rows) for i in order):
            return rows  # no answer, or one that does not describe what was sent: the order stands
        rest = [r for i, r in enumerate(rows) if i not in set(order)]
        return [rows[i] for i in order] + rest

    def _passage_rows(self, hits: Iterable[tuple[str, int]]) -> list[dict]:
        """Search rows for (item_id, passage) pairs, best first, one per item. A pair
        the file no longer has (vectors a sync behind) is skipped."""
        out: list[dict] = []
        seen: set[str] = set()
        for item_id, ord_ in hits:
            if item_id in seen:
                continue
            r = self.db.execute(
                "SELECT c.item_id, c.ord, c.body, i.type, i.title, i.url, i.date FROM chunks c "
                "JOIN items i ON i.id = c.item_id WHERE c.item_id = ? AND c.ord = ?", (item_id, ord_)).fetchone()
            if r is None:
                continue
            seen.add(item_id)
            body = " ".join((r["body"] or "").split())
            out.append({"id": r["item_id"], "passage": r["ord"], "type": r["type"], "title": r["title"],
                        "url": r["url"], "date": r["date"],
                        "snippet": body[:SNIPPET_CHARS] + (" …" if len(body) > SNIPPET_CHARS else "")})
        return out

    def passages(self) -> Iterator[dict]:
        """Every passage with what its vector is made of (`hybrid.embedding_text`)
        and keyed by (`hybrid.passage_sha`), in file order. For a host's sync.

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

    def _lexical(self, query: str, types: list[str] | None, since: str, until: str,
                 limit: int) -> list[dict]:
        """BM25 over the FTS index. AND first; falls back to OR when that is too narrow."""
        out: list[dict] = []
        seen: set[tuple] = set()
        for mode in ("AND", "OR"):
            fq = self._fts_query(query, mode)
            if not fq:
                break
            sql = ("SELECT c.item_id, c.ord, i.type, i.title, i.url, i.date, "
                   "snippet(chunks_fts, 3, '«', '»', ' … ', 40) AS snip, bm25(chunks_fts, 5.0, 2.0, 1.0, 1.0) AS score "
                   "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN items i ON i.id = c.item_id "
                   "WHERE chunks_fts MATCH ?")
            args: list[Any] = [fq]
            if types:
                sql += f" AND i.type IN ({','.join('?' * len(types))})"
                args += types
            if since:
                sql += " AND i.date >= ?"
                args.append(since)
            if until:
                sql += " AND i.date <= ?"
                args.append(until)
            boost = self.manifest.get("search_boost") or {}
            if boost:  # bm25 is negative (lower = better), so a factor > 1 ranks a type higher
                case = " ".join("WHEN ? THEN ?" for _ in boost)
                sql = f"SELECT * FROM ({sql}) ORDER BY score * (CASE type {case} ELSE 1.0 END) LIMIT ?"
                for t, f in boost.items():
                    args += [t, float(f)]
            else:
                sql += " ORDER BY score LIMIT ?"
            args.append(limit * 4)
            for r in self.db.execute(sql, args):
                key = (r["item_id"],)
                if key in seen:
                    continue  # one passage per item keeps the list diverse
                seen.add(key)
                out.append({"id": r["item_id"], "passage": r["ord"], "type": r["type"], "title": r["title"],
                            "url": r["url"], "date": r["date"], "snippet": r["snip"]})
                if len(out) >= limit:
                    return out
        return out

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
