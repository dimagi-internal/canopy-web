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

import threading
import time

from canopy_kb.prep.raw import UA, _stable, now, sha

KEEP = (200, 401, 403, 404, 410)
SECTION = "fetch"
#: Bytes held in memory before new ones are stored and the cache dropped.
HOLD_BYTES = 512 * 1024**2


def stable(url: str, ctype: str, body: bytes) -> str:
    """The hash of what an export says (its volatile parts left out), else ""."""
    s = _stable(url, ctype, body)
    return s.hex() if s is not None else ""


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
                    row = {**old, "checked_at": now()}
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
                if old.get("status") == 200 and r.status_code == 200 and (
                        old.get("sha256") == digest or (st and old.get("stable") == st)):
                    # Same bytes, or an export that says the same: keep the row (and its
                    # original), so nothing downstream is re-read or re-published.
                    row = {**old, "checked_at": now()}
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
