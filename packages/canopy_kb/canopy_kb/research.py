"""The research tools: what an agent uses to find, open and verify, as compact text.

An agent answering a hard question does not want ten 300-character snippets; it wants
to orient (`overview`, `entity`), find the right documents (`search`, which shows each
document's catalog card and its best passages), open them (`doc` for the outline,
links and versions, `read` for a section, `grep` for a number or quote) and compute
(`sql` over the grants and other tables). Results are plain text: an agent reads it
directly, and it costs a fraction of the tokens JSON would.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Callable, Optional, Sequence

if TYPE_CHECKING:
    from canopy_kb.kb import KB
    from canopy_kb.vectors import Semantic

#: `rerank(query, documents)` → indices into `documents`, best first, or None when it
#: could not answer (the word order then stands). A document is a dict with `title`
#: and `text`. canopy-web's is the Vertex AI ranking API (`apps/agents/kb_rerank.py`).
Rerank = Callable[[str, list[dict]], Optional[Sequence[int]]]

#: `link(sha256, filename, content_type)` → a short-lived URL that downloads that original,
#: or None when the host does not hold it. canopy-web's signs a GET on its store
#: (`apps/agents/kb_store.py`).
OriginalLink = Callable[[str, str, str], Optional[str]]
#: How long a host's original link should stay valid; `original` tells the reader.
LINK_MINUTES = 15

#: Passages pooled per search before reranking, and per document within that pool.
POOL = 90
PER_ITEM = 3
#: What a reranker sees of each candidate (the ranking models read ~1k tokens).
RERANK_CHARS = 4000
READ_CHARS = 15000
READ_MAX = 60000
SQL_SECONDS = 5.0
STOP = frozenset("""a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having
he her here hers him his how i if in into is it its just me more most my no nor not now of off on once only or
other our out over own same she should so some such than that the their them then there these they this those
through to too under until up very was we were what when where which while who whom why will with would you
your""".split())


def clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _jl(s) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fts_query(q: str, mode: str) -> str:
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    terms = [t.strip(".-") for t in re.findall(r"[\w$%.-]+", rest)]
    terms = [t for t in terms if t and (len(t) > 1 or t.isdigit())]
    content = [t for t in terms if t.lower() not in STOP] or terms
    parts = ['"' + p.replace('"', "") + '"' for p in phrases] + ['"' + t + '"' for t in content]
    return f" {mode} ".join(parts)


def find_entities(kb: "KB", name: str) -> list[sqlite3.Row]:
    """Entities whose name or an alias is `name` (first), then those containing it."""
    k = _norm(name)
    if not k:
        return []
    exact, part = [], []
    for r in kb.db.execute("SELECT * FROM entities ORDER BY n_items DESC"):
        als = [_norm(x) for x in _jl(r["aliases"])]
        joined = " | ".join(als)
        if k in als:
            exact.append(r)
        elif k in joined or (len(k) > 3 and all(w in joined for w in k.split())):
            part.append(r)
    return exact + part


def _filters(kb: "KB", types=None, entity: str = "", since: str = "", until: str = "") -> tuple[str, list]:
    sql, args = [], []
    if types:
        types = [types] if isinstance(types, str) else list(types)
        sql.append(f"i.type IN ({','.join('?' * len(types))})")
        args += types
    if since:
        sql.append("i.date >= ?")
        args.append(since)
    if until:
        sql.append("i.date != '' AND i.date <= ?")
        args.append(until + "￿")
    if entity:
        ids = [e["id"] for e in find_entities(kb, entity)[:3]]
        if not ids:
            return "0", []
        sql.append(f"i.id IN (SELECT item_id FROM item_entities WHERE entity_id IN ({','.join('?' * len(ids))}))")
        args += ids
    return (" AND ".join(sql) or "1"), args


def snippet(body: str, q: str, n: int = 600) -> str:
    terms = [t for t in re.findall(r"\w+", q.lower()) if t not in STOP and len(t) > 2]
    low = body.lower()
    pos = min([p for p in (low.find(t) for t in terms) if p >= 0] or [0])
    a = max(0, pos - n // 3)
    return ("…" if a else "") + clip(body[a:a + n], n)


# -- search ------------------------------------------------------------------------------
_PASSAGE_SQL = ('SELECT c.id, c.item_id, c.ord, c.start, c.heading, c.context, c.body, i.title, i.type, i.date, i.url '
                'FROM chunks c JOIN items i ON i.id = c.item_id ')


def search(kb: "KB", query: str, types=None, entity: str = "", since: str = "", until: str = "", k: int = 10,
           semantic: "Semantic | None" = None, rerank: "Rerank | None" = None) -> str:
    """Documents for a question, best first: each with its card and best passages.

    Candidates are passages matching the words (all of them, then any) and documents
    whose catalog card matches; a reranker then orders the whole pool by meaning.
    `semantic` is the dormant vector hook (`vectors.py`): no host passes one today."""
    if not query.strip():
        return "Empty query."
    k = max(1, min(int(k or 10), 30))
    where, args = _filters(kb, types, entity, since, until)
    cands: list[dict] = []
    per: dict[str, int] = {}
    ids: set[int] = set()

    def take(r) -> bool:
        if r["id"] in ids or per.get(r["item_id"], 0) >= PER_ITEM:
            return False
        ids.add(r["id"])
        per[r["item_id"]] = per.get(r["item_id"], 0) + 1
        cands.append(dict(r))
        return True

    for mode in ("AND", "OR"):
        fq = fts_query(query, mode)
        if not fq:
            break
        sql = (_PASSAGE_SQL.replace("FROM chunks c", "FROM chunks_fts f JOIN chunks c ON c.id = f.rowid") +
               f"WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts, 2, 2, 1, 1) LIMIT 300")
        for r in kb.db.execute(sql, [fq] + args):
            take(r)
            if len(cands) >= POOL:
                break
        if len(cands) >= POOL * 2 // 3:
            break
    if semantic is not None:
        near = semantic(query, types=[types] if isinstance(types, str) else (types or None),
                        since=since, until=until, limit=50) or []
        allowed = None
        if entity:
            allowed = {r[0] for r in kb.db.execute(f"SELECT i.id FROM items i WHERE {where}", args)}
        for item_id, ord_ in near:
            if allowed is not None and item_id not in allowed:
                continue
            r = kb.db.execute(_PASSAGE_SQL + "WHERE c.item_id = ? AND c.ord = ?", (item_id, ord_)).fetchone()
            if r:
                take(r)
    cards: list[dict] = []
    fq = fts_query(query, "OR")
    if fq:
        sql = (f"SELECT f.item_id, i.title, i.type, i.date, i.url, cd.summary FROM cards_fts f "
               f"JOIN items i ON i.id = f.item_id JOIN cards cd ON cd.item_id = f.item_id "
               f"WHERE cards_fts MATCH ? AND {where} ORDER BY bm25(cards_fts, 0, 3, 1, 1, 2) LIMIT 30")
        cards = [dict(r) | {"card": True} for r in kb.db.execute(sql, [fq] + args) if r["item_id"] not in per]
    pool = cands + cards
    if not pool:
        return f"No matches for {query!r}. Try other words or synonyms, fewer words, or `entity`."
    order = list(range(len(pool)))
    if rerank is not None and len(pool) > 1:
        recs = [{"title": (f"{p['title']} — {p['heading']}" if p.get("heading") else p["title"]) or "",
                 "text": ((p.get("context") or "") + "\n" + (p.get("body") or p.get("summary") or ""))[:RERANK_CHARS]}
                for p in pool]
        got = list(rerank(query, recs) or [])
        if got and len(set(got)) == len(got) and all(0 <= i < len(pool) for i in got):
            order = got + [i for i in order if i not in set(got)]
    grouped: dict[str, list[dict]] = {}
    for i in order:
        p = pool[i]
        if p["item_id"] not in grouped and len(grouped) >= k:
            continue
        g = grouped.setdefault(p["item_id"], [])
        if len(g) < 2:
            g.append(p)
    top = list(grouped)
    cardrows = {r["item_id"]: r for r in kb.db.execute(
        f"SELECT item_id, kind, summary FROM cards WHERE item_id IN ({','.join('?' * len(top))})", top)}
    out = []
    for n, iid in enumerate(top, 1):
        ps = grouped[iid]
        p0 = ps[0]
        out.append(f"[{n}] {iid} · {p0['type']} · {p0['date'] or 'undated'} · {clip(p0['title'], 110)}\n    {p0['url']}")
        c = cardrows.get(iid)
        if c:
            out.append(f"    card ({c['kind']}): {clip(c['summary'], 320)}")
        for p in ps:
            if p.get("card"):
                continue
            h = f"§ {clip(p['heading'], 90)} " if p.get("heading") else ""
            out.append(f"    ▸ {h}(at {p['start']}): {snippet(p['body'] or '', query)}")
    return "\n".join(out)


# -- orientation -------------------------------------------------------------------------
def overview(kb: "KB") -> str:
    m, s = kb.manifest, kb.summary
    out = [f"{m.get('name', 'knowledge base')} — {m.get('publisher', '')}. Licence: {m.get('license', '')}. "
           f"Built {s.get('built_at', '')}: {s.get('items', 0):,} documents, {s.get('passages', 0):,} passages.",
           "", "Documents by type (type · count · date range):"]
    for r in kb.db.execute("SELECT type, count(*) n, min(nullif(date,'')) a, max(date) b FROM items GROUP BY type ORDER BY n DESC"):
        out.append(f"  {r['type']:<20} {r['n']:>6}  {r['a'] or ''} – {r['b'] or ''}")
    for kind, label in (("org", "organisations"), ("program", "programs")):
        top = kb.db.execute("SELECT name, n_items FROM entities WHERE kind = ? ORDER BY n_items DESC LIMIT 40",
                            (kind,)).fetchall()
        if top:
            out.append(f"\nMost-covered {label}: " + "; ".join(f"{r['name']} ({r['n_items']})" for r in top))
    out.append("\nTables for `sql` (columns):")
    for name, spec in kb.tables().items():
        tn = spec["sql_table"]
        cols = [r[1] for r in kb.db.execute(f'PRAGMA table_info("{tn}")')]
        out.append(f"  {tn} ({spec.get('rows') or 0} rows): {', '.join(cols)} — {spec.get('description', '')}")
    out.append("  items (no text column via sql; use read), cards, entities, item_entities(item_id, entity_id), "
               "links(src, dst), sections(item_id, ord, level, path, start, end)"
               + (", originals(item_id, sha256, filename, content_type, bytes, source_url, fetched_at, "
                  "first_seen, superseded_at)" if kb.has_originals() else ""))
    out.append("""
How to work: `entity` for a named organisation or program; `search` for topics (try several phrasings —
a miss is not absence); open what matters with `doc` (outline, links, versions) and `read` (a section);
`grep` a long document or spreadsheet for a number or name; `sql` for amounts, dates and counts.""")
    if kb.has_originals():
        out.append("Each document's text was read from an original file (PDF, spreadsheet, page); `original` hands "
                   "you that file — to recompute a model, see a chart, or quote a version the publisher has since "
                   "changed.")
    guide = m.get("guide")
    if guide:
        out.append("\nAbout this library:\n" + "\n".join(f"- {g}" for g in (guide if isinstance(guide, list) else [guide])))
    return "\n".join(out)


def entity(kb: "KB", name: str, type_: str = "") -> str:
    ents = find_entities(kb, name)
    if not ents:
        return f"No organisation, program or country matches {name!r}. Try `search`."
    e = ents[0]
    out = [f"{e['name']} ({e['kind']}) — {e['n_items']} documents; also known as: {', '.join(_jl(e['aliases'])[:15])}"]
    if len(ents) > 1:
        out.append("other matches: " + "; ".join(f"{x['name']} ({x['kind']}, {x['n_items']})" for x in ents[1:8]))
    rows = kb.db.execute(
        "SELECT i.id, i.type, i.title, i.date, cd.summary FROM item_entities ie JOIN items i ON i.id = ie.item_id "
        "LEFT JOIN cards cd ON cd.item_id = i.id WHERE ie.entity_id = ? " + ("AND i.type = ? " if type_ else "") +
        "ORDER BY i.date DESC", [e["id"]] + ([type_] if type_ else [])).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["type"], []).append(r)
    per = 400 if type_ else 12
    for typ, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        out.append(f"\n## {typ} ({len(rs)})")
        out += [f"- {r['id']} · {r['date'] or 'undated'} · {clip(r['title'], 90)} — {clip(r['summary'], 150)}"
                for r in rs[:per]]
        if len(rs) > per:
            out.append(f"  … {len(rs) - per} more: entity {name!r} with type {typ!r}")
    amount_tables = [(n, s) for n, s in kb.tables().items() if s.get("amount_column")]
    if e["kind"] == "org" and amount_tables:
        pats = [f"%{a}%" for a in _jl(e["aliases"]) if len(a) > 3][:8] or [f"%{e['name']}%"]
        for name_, spec in amount_tables:
            cols = {r[1] for r in kb.db.execute(f'PRAGMA table_info("{spec["sql_table"]}")')}
            who = next((c for c in ("Recipient", "recipient", "Grantee", "grantee", "Organization") if c in cols), None)
            if not who:
                continue
            date_c, amt_c = spec.get("date_column") or "rowid", spec["amount_column"]
            g = kb.db.execute(
                f'SELECT * FROM "{spec["sql_table"]}" WHERE {" OR ".join([f"{chr(34)}{who}{chr(34)} LIKE ?"] * len(pats))} '
                f'ORDER BY "{date_c}" DESC', pats).fetchall()
            if g:
                total = sum(float(r[amt_c] or 0) for r in g if isinstance(r[amt_c], (int, float)))
                out.append(f"\n## {name_} table ({len(g)} rows, {total:,.0f} total {amt_c})")
                for r in g[:30]:
                    d = dict(r)
                    label = d.get("Grant") or d.get("title") or d.get(who)
                    url = d.get("url") or d.get("Link to grant description") or ""
                    out.append(f"- {d.get(date_c, '')} · {float(d[amt_c] or 0):,.0f} · {clip(str(label), 90)} {url}")
    return "\n".join(out)


# -- documents ---------------------------------------------------------------------------
_VERSION = re.compile(r"\s*[-–—:]\s*(?:[A-Z][a-z]+ \d{4}|\d{4})\s*(?:archived\s*)?version\b", re.I)


def doc(kb: "KB", ref: str) -> str:
    it = kb.item_row(ref)
    if not it:
        return f"No document {ref!r}."
    iid = it["id"]
    meta = json.loads(it["meta"] or "{}")
    out = [f"{iid} · {it['type']} · {it['date'] or 'undated'} · {it['chars']:,} chars\n{it['title']}\n{it['url']}"]
    alt = sorted(set(meta.get("also_at") or []) - {it["url"]})
    if alt:
        out.append("also at: " + ", ".join(alt[:5]))
    origs = kb.originals(iid)
    if origs:
        o = origs[0]
        line = (f"original: {o['filename']} · {o['content_type']} · {_size(o['bytes'])} · fetched "
                f"{(o['fetched_at'] or '')[:10]}" + (" (no longer served; see original)" if o["superseded_at"] else ""))
        if len(origs) > 1:
            line += f" · {len(origs) - 1} earlier version{'s' if len(origs) > 2 else ''}"
        out.append(line + f" — `original {iid}` for the file")
    c = kb.db.execute("SELECT * FROM cards WHERE item_id = ?", (iid,)).fetchone()
    if c:
        out.append(f"\nkind: {c['kind']}; period: {c['period']}\nsummary: {c['summary']}")
        for f in ("orgs", "programs", "countries"):
            if _jl(c[f]):
                out.append(f"{f}: {', '.join(_jl(c[f]))}")
        if c["superseded"]:
            out.append(f"superseded: {c['superseded']}")
    secs = kb.db.execute('SELECT ord, level, path, start, "end" FROM sections WHERE item_id = ? ORDER BY ord', (iid,)).fetchall()
    if secs:
        out.append(f"\noutline ({len(secs)} sections; read one with section=N):")
        show = secs if len(secs) <= 80 else [s for s in secs if s["level"] <= 2][:80]
        for s in show:
            out.append(f"  {s['ord']:>3} {'  ' * (s['level'] - 1)}{clip(s['path'].split(' › ')[-1], 80)}"
                       f"  [at {s['start']}, {s['end'] - s['start']:,} chars]")
        if len(show) < len(secs):
            out.append(f"  … {len(secs) - len(show)} deeper sections not shown")
    for label, sql in (("links to", "SELECT i.id, i.type, i.title FROM links l JOIN items i ON i.id = l.dst WHERE l.src = ?"),
                       ("linked from", "SELECT i.id, i.type, i.title FROM links l JOIN items i ON i.id = l.src WHERE l.dst = ?")):
        rows = kb.db.execute(sql, (iid,)).fetchall()
        if rows:
            out.append(f"\n{label} ({len(rows)}):")
            out += [f"  - {r['id']} · {r['type']} · {clip(r['title'], 90)}" for r in rows[:40]]
    stem = _VERSION.split(it["title"] or "")[0].strip()
    if len(stem) >= 8:
        vers = kb.db.execute("SELECT id, title, date FROM items WHERE id != ? AND type = ? AND title LIKE ? "
                             "ORDER BY date DESC LIMIT 15", (iid, it["type"], stem.replace("%", "") + "%")).fetchall()
        if vers:
            out.append("\nother versions (same title):")
            out += [f"  - {r['id']} · {r['date'] or 'undated'} · {clip(r['title'], 90)}" for r in vers]
    rel = kb.db.execute("""SELECT i.id, i.type, i.title, i.date, count(*) shared FROM item_entities a
        JOIN item_entities b ON b.entity_id = a.entity_id AND b.item_id != a.item_id
        JOIN entities e ON e.id = a.entity_id AND e.kind != 'country' AND e.n_items < 400
        JOIN items i ON i.id = b.item_id WHERE a.item_id = ? GROUP BY i.id
        HAVING shared >= 2 ORDER BY shared DESC, i.date DESC LIMIT 12""", (iid,)).fetchall()
    if rel:
        out.append("\nrelated (same organisations/programs):")
        out += [f"  - {r['id']} · {r['type']} · {r['date'] or 'undated'} · {clip(r['title'], 80)}" for r in rel]
    return "\n".join(out)


def _size(n) -> str:
    n = int(n or 0)
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


def original(kb: "KB", ref: str, version: int = 0, link: "OriginalLink | None" = None) -> str:
    """The file a document's text was read from, as the publisher served it: a link to
    download it (the workbook to recompute, the PDF's charts and layout, a copy the
    publisher has since changed or removed), and its earlier versions."""
    it = kb.item_row(ref)
    if not it:
        return f"No document {ref!r}."
    origs = kb.originals(it["id"])
    if not origs:
        return f"{it['id']} has no original file in this library (its text is the source); the publisher's copy: {it['url']}"
    version = int(version or 0)
    if not 0 <= version < len(origs):
        return f"{it['id']} has versions 0–{len(origs) - 1}; 0 is the newest."
    o = origs[version]
    out = [f"{it['id']} · {clip(it['title'], 100)}",
           f"{'current' if not o['superseded_at'] else 'superseded ' + o['superseded_at'][:10]} original "
           f"(version {version}): {o['filename']} · {o['content_type']} · {_size(o['bytes'])} · fetched "
           f"{(o['fetched_at'] or '')[:10]} from {o['source_url']} · sha256 {o['sha256']}"]
    drive = (json.loads(it["meta"] or "{}").get("drive") or {}) if it["meta"] else {}
    url = link(o["sha256"], o["filename"], o["content_type"]) if link and not drive else None
    if drive:
        # A Drive source's files are not copied: the reader opens the file in Drive, with
        # the access the folder's owner gave them. The revision is the one indexed.
        out.append(f"Google Drive file (revision {drive.get('revision') or '?'} indexed; Drive shows the latest, "
                   f"to people the folder is shared with): {o['source_url']}")
    elif url:
        out.append(f"download (valid {LINK_MINUTES} minutes): {url}")
    else:
        out.append(f"This server does not hand out files; the publisher's copy (may have changed): {o['source_url']}")
    if len(origs) > 1:
        out.append("\nversions (pass version=N):")
        out += [f"  {n}. {(r['fetched_at'] or '')[:10]} · {r['filename']} · {_size(r['bytes'])}"
                + (f" · superseded {r['superseded_at'][:10]}" if r["superseded_at"] else " · current")
                for n, r in enumerate(origs)]
    return "\n".join(out)


def read(kb: "KB", ref: str, section: int | None = None, at: int = 0, chars: int = READ_CHARS) -> str:
    it = kb.item_row(ref)
    if not it:
        return f"No document {ref!r}."
    total = it["chars"] or 0
    n = max(500, min(int(chars or READ_CHARS), READ_MAX))
    start, end = max(0, int(at or 0)), None
    if section is not None:
        s = kb.db.execute('SELECT start, "end" FROM sections WHERE item_id = ? AND ord = ?', (it["id"], int(section))).fetchone()
        if not s:
            return f"No section {section} in {it['id']}; see `doc {it['id']}` for the outline."
        start, end = s["start"], s["end"]
    end = min(total, start + n if end is None else min(end, start + n))
    body = kb.text(it["id"], start, end)
    if end < total:
        nl = body.rfind("\n", n // 2)
        if nl > 0:
            body, end = body[:nl], start + nl
    head = f"{it['id']} · {clip(it['title'], 100)} · {it['url']}\nchars {start:,}–{end:,} of {total:,}"
    tail = f"\n[continue with at={end}]" if end < total else "\n[end of document]"
    return f"{head}\n\n{body}{tail}"


def grep(kb: "KB", pattern: str, ref: str = "", entity: str = "", limit: int = 40) -> str:
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return f"Bad pattern: {e}"
    if entity:
        ents = find_entities(kb, entity)[:1]
        if not ents:
            return f"No entity matches {entity!r}."
        rows = kb.db.execute("SELECT i.id, i.title FROM item_entities ie JOIN items i ON i.id = ie.item_id "
                             "WHERE ie.entity_id = ? ORDER BY i.date DESC LIMIT 400", (ents[0]["id"],)).fetchall()
    else:
        r = kb.item_row(ref)
        if not r:
            return f"No document {ref!r}."
        rows = [r]
    limit = max(1, min(int(limit or 40), 200))
    out, total = [], 0
    for r in rows:
        hits, pos = [], 0
        for line in kb.text(r["id"]).split("\n"):
            if rx.search(line):
                hits.append(f"  (at {pos}) {clip(line, 300)}")
            pos += len(line) + 1
        if hits:
            room = limit - total
            out.append(f"{r['id']} · {clip(r['title'], 90)} — {len(hits)} matching lines")
            out += hits[:room]
            total += min(len(hits), room)
        if total >= limit:
            out.append("[more matches not shown; narrow the pattern]")
            break
    return "\n".join(out) or f"No line matches /{pattern}/."


def sql(kb: "KB", query: str, limit: int = 200) -> str:
    if not re.match(r"\s*(select|with)\b", query or "", re.I):
        return "Only SELECT (or WITH … SELECT) queries."
    deadline = time.monotonic() + SQL_SECONDS
    kb.db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        cur = kb.db.execute(query)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(int(limit) + 1)
    except sqlite3.Error as e:
        return f"SQL error: {e}" + (" (stopped after 5 s; narrow the query)" if "interrupt" in str(e) else "")
    finally:
        kb.db.set_progress_handler(None, 0)
    lines = [" | ".join(cols)] + [" | ".join(clip(str(v), 300) for v in r) for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"[first {limit} rows]")
    return "\n".join(lines)
