"""HTTP fetching for a refresh with no local state (`canopy_kb.curate`).

The fetch index lives in the curation state (section "fetch": one row per url, as
`raw.Raw` kept it, without a local file), and the bytes in the host's originals store
by sha256. So a refresh on a machine that has never seen the corpus asks the publisher
only what it would have asked on the machine that ran the last one:

* a url whose row says it need not be refetched is not requested at all;
* a refetch is conditional (the row's ETag / Last-Modified), and a 304 keeps the row;
* a Google export re-made with new bytes but the same content (`raw.same_content`'s
  stable hash, kept on the row) keeps its row, sha and original;
* a fetch that fails keeps the last good row: an outage is not a deletion;
* each url keeps the `seq` it was first fetched in (`in_order`): the state is a dict
  saved in chunks and comes back in another order, and a curator whose ties go to
  the first-seen document must see the same order on every machine.

New bytes go to the originals store as they arrive, in batches (`flush`: a first crawl
is gigabytes, which no runner holds in memory); bytes of an unchanged url come from
the originals store, and only when a step that has no result for them yet needs them.
"""
from __future__ import annotations

import hashlib
import io
import threading
import time
import zipfile

from canopy_kb.prep.raw import UA, _stable, now, sha

KEEP = (200, 401, 403, 404, 410)
SECTION = "fetch"
#: Bytes held in memory before new ones are stored and the cache dropped.
HOLD_BYTES = 512 * 1024**2


def stable(url: str, ctype: str, body: bytes) -> str:
    """The hash of what an export says (its volatile parts left out), else "". A
    workbook's is of its cells (`cells_hash`); a Google Doc's of its HTML less what the
    exporter regenerates (`raw._stable`)."""
    if body[:2] == b"PK" and "spreadsheetml" in ctype:
        h = cells_hash(body)
        if h:
            return h
    s = _stable(url, ctype, body)
    return s.hex() if s is not None else ""


def _legacy(url: str, ctype: str, body: bytes) -> str:
    s = _stable(url, ctype, body)
    return s.hex() if s is not None else ""


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}


def cells_hash(body: bytes) -> str:
    """A workbook's content: per sheet (in workbook order), every cell's reference, type,
    value (shared strings resolved), formula and number format, its hyperlinks, and every
    note. Google re-exports an unchanged Sheet with its shared-string and style tables
    renumbered (measured 2026-10-23: 21 of 40 GiveWell sheets on one day), which changes
    every byte and nothing a reader shows. "x2:" + sha256, or "" when unreadable."""
    import xml.etree.ElementTree as ET
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
        names = set(z.namelist())

        def xml(name):
            return ET.fromstring(z.read(name)) if name in names else None

        def text(el) -> str:
            return "".join(t.text or "" for t in el.iter(f"{{{_NS['m']}}}t"))

        sst = [text(si) for si in (xml("xl/sharedStrings.xml") or ET.Element("x")).findall("m:si", _NS)]
        styles = xml("xl/styles.xml")
        fmts: dict[str, str] = {}
        xf_fmt: list[str] = []
        if styles is not None:
            for nf in styles.findall("m:numFmts/m:numFmt", _NS):
                fmts[nf.get("numFmtId")] = nf.get("formatCode") or ""
            for xf in styles.findall("m:cellXfs/m:xf", _NS):
                fid = xf.get("numFmtId") or "0"
                xf_fmt.append(fmts.get(fid, f"builtin:{fid}"))
        rels = {r.get("Id"): r.get("Target") for r in (xml("xl/_rels/workbook.xml.rels") or ET.Element("x"))
                .findall("pr:Relationship", _NS)}
        h = hashlib.sha256()
        for sh in xml("xl/workbook.xml").findall("m:sheets/m:sheet", _NS):
            target = rels.get(sh.get(f"{{{_NS['r']}}}id")) or ""
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            h.update(b"\x01" + (sh.get("name") or "").encode())
            ws = xml(path)
            if ws is None:
                continue
            for c in ws.iter(f"{{{_NS['m']}}}c"):
                t, v, f = c.get("t") or "", c.find("m:v", _NS), c.find("m:f", _NS)
                val = v.text if v is not None else ""
                if t == "s" and val:
                    val = sst[int(val)]
                elif t == "inlineStr":
                    val = text(c)
                s_ = c.get("s")
                fmt = xf_fmt[int(s_)] if s_ and s_.isdigit() and int(s_) < len(xf_fmt) else ""
                form = (f.text or "") + "|" + "|".join(f"{k}={f.get(k)}" for k in sorted(f.attrib)) if f is not None else ""
                h.update("\x02".join((c.get("r") or "", t, val or "", form, fmt)).encode() + b"\x03")
            srels_name = path.rsplit("/", 1)[0] + "/_rels/" + path.rsplit("/", 1)[1] + ".rels"
            srels = {r.get("Id"): r.get("Target") for r in (xml(srels_name) or ET.Element("x"))
                     .findall("pr:Relationship", _NS)}
            rid = f"{{{_NS['r']}}}id"
            for hl in ws.findall("m:hyperlinks/m:hyperlink", _NS):
                h.update(f"link:{hl.get('ref')}:{srels.get(hl.get(rid), hl.get('location') or '')}".encode())
        notes = []
        for name in sorted(n for n in names if n.startswith("xl/comments") and n.endswith(".xml")):
            for cm in xml(name).findall("m:commentList/m:comment", _NS):
                notes.append(f"{cm.get('ref')}:{text(cm)}")
        for n in sorted(notes):
            h.update(b"note:" + n.encode())
        return "x2:" + h.hexdigest()
    except Exception:  # noqa: BLE001 - not a workbook we can read: compare its bytes instead
        return ""


def _recovered(row: dict) -> dict:
    """`row` without the `last_error` an earlier outage left: this fetch answered, so the
    error is history. Kept, it reads as a live failure to anything keyed on it (eva's
    Drive-version check re-exports a file with an error mark on every refresh)."""
    return {k: v for k, v in row.items() if k != "last_error"}


class Fetch:
    """`raw.Raw`'s cached GET, over the curation state instead of a data directory."""

    def __init__(self, cur, ua: str = UA):
        self.cur = cur
        self.ua = ua
        self.index: dict[str, dict] = cur.state.section(SECTION)
        self._bodies: dict[str, bytes] = {}     # sha256 -> bytes in hand (a cache)
        self._new: dict[str, str] = {}          # sha256 -> content type, not yet stored
        self._held = 0
        self.stored = 0
        self._lock = threading.Lock()
        self._local = threading.local()
        self.counts = {"requests": 0, "not_modified": 0, "same_content": 0, "new": 0, "errors_kept": 0}
        self._seq = max((r.get("seq", -1) for r in self.index.values()), default=-1) + 1

    def in_order(self) -> list[tuple[str, dict]]:
        """(url, row) in the order the urls were first fetched."""
        return sorted(self.index.items(), key=lambda kv: (kv[1].get("seq", float("inf")), kv[0]))

    def _client(self):
        import httpx
        if not hasattr(self._local, "c"):
            self._local.c = httpx.Client(timeout=120, headers={"User-Agent": self.ua}, follow_redirects=True)
        return self._local.c

    def get(self, url: str, refetch: bool = False, ua: str | None = None) -> dict:
        """The url's row, fetched when it has none (or a failed one), or when `refetch`."""
        old = self.index.get(url) or {}
        if not refetch and old.get("status") in KEEP:
            return old
        cond = {}
        if old.get("status") == 200:
            if old.get("etag"):
                cond["If-None-Match"] = old["etag"]
            if old.get("last_modified"):
                cond["If-Modified-Since"] = old["last_modified"]
        row: dict = {}
        for attempt in range(5):
            try:
                with self._lock:
                    self.counts["requests"] += 1
                r = self._client().get(url, headers={**({"User-Agent": ua} if ua else {}), **cond} or None)
                if r.status_code == 304 and cond:
                    row = {**_recovered(old), "checked_at": now()}
                    self._count("not_modified")
                    break
                if r.status_code in (429, 500, 502, 503, 504):
                    row = {"url": url, "status": r.status_code}
                    time.sleep(2 ** attempt * 3)
                    continue
                body = r.content
                ctype = r.headers.get("content-type", "")
                digest = sha(body)
                st = stable(url, ctype, body)
                said = {st, _legacy(url, ctype, body)} - {""}  # a row from before cells_hash
                if old.get("status") == 200 and r.status_code == 200 and (
                        old.get("sha256") == digest or (old.get("stable") in said)):
                    # Same bytes, or an export that says the same: keep the row (and its
                    # original), so nothing downstream is re-read or re-published.
                    row = {**_recovered(old), "checked_at": now(), **({"stable": st} if st else {})}
                    self._count("same_content")
                    break
                row = {"url": url, "final_url": str(r.url), "status": r.status_code, "ctype": ctype,
                       "cd": r.headers.get("content-disposition", ""), "bytes": len(body), "sha256": digest,
                       "fetched_at": now()}
                if st:
                    row["stable"] = st
                for h, k in (("etag", "etag"), ("last-modified", "last_modified")):
                    if r.headers.get(h):
                        row[k] = r.headers[h]
                if r.status_code == 200:
                    self._hold(digest, body, ctype)
                self._count("new")
                break
            except Exception as e:  # noqa: BLE001 - a network error is retried, then recorded
                row = {"url": url, "status": -1, "error": str(e)[:200]}
                time.sleep(2 ** attempt * 2)
        if row.get("status") not in KEEP and old.get("status") == 200:
            # An outage, a 5xx or a timeout: keep what we had, and say when we tried.
            row = {**old, "checked_at": now(), "last_error": row.get("error") or row.get("status")}
            self._count("errors_kept")
        with self._lock:
            if "seq" in old:
                row["seq"] = old["seq"]
            else:
                row["seq"], self._seq = self._seq, self._seq + 1
            self.index[url] = row
        return row

    def _hold(self, digest: str, body: bytes, ctype: str | None = None) -> None:
        with self._lock:
            if digest not in self._bodies:
                self._bodies[digest] = body
                self._held += len(body)
            if ctype is not None:
                self._new[digest] = (ctype or "application/octet-stream").split(";")[0].strip()
            full = self._held > HOLD_BYTES
        if full:
            self.flush()

    def flush(self) -> int:
        """Store the new bytes in hand as originals and drop the cache. Returns how many
        the store did not have yet."""
        with self._lock:
            files = {d: (self._bodies[d], t) for d, t in self._new.items() if d in self._bodies}
            self._new = {}
        n = self.cur.put_originals(files) if files else 0
        with self._lock:
            self.stored += n
            self._bodies, self._held = {}, 0
        return n

    def _count(self, k: str) -> None:
        with self._lock:
            self.counts[k] += 1

    def body(self, row: dict) -> bytes:
        """A row's bytes: fetched this run, else from the originals store."""
        digest = row.get("sha256")
        if not digest or row.get("status") != 200:
            return b""
        body = self._bodies.get(digest)
        if body is None:
            got = self.cur.originals([digest])
            if digest not in got:
                raise KeyError(f"{row.get('url')}: original {digest[:12]} is not stored")
            body = got[digest]
            self._hold(digest, body)
        return body

    def prefetch(self, rows: list[dict]) -> None:
        """Fetch the stored originals of many rows in one go (before a parallel step)."""
        want = sorted({r["sha256"] for r in rows if r.get("status") == 200 and r.get("sha256")
                       and r["sha256"] not in self._bodies})
        for i in range(0, len(want), 200):
            for d, b in self.cur.originals(want[i:i + 200]).items():
                self._hold(d, b)

    def new_originals(self) -> dict[str, tuple[bytes, str]]:
        """New bytes in hand that are not stored yet."""
        with self._lock:
            return {d: (self._bodies[d], t) for d, t in self._new.items() if d in self._bodies}

    def forget_bodies(self) -> None:
        """Store what is new, then drop the cache (a long refresh holds only what it uses)."""
        self.flush()
