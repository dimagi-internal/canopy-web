"""Curation with no local state: a refresh that runs from an empty disk on any machine.

A knowledge base's host (canopy-web) keeps everything a refresh needs from the last
one, so a runner's disk is scratch (canopy-web#1925):

* **Step results.** Every derived thing (an extracted text, an OCR transcription, a
  catalog card, the alias groups) is stored under `Step.key(inputs)`: the sha256 of the
  step's name, its fingerprint (REVISION, prompt, model, settings) and its inputs'
  hashes. Change a prompt and every key changes; an unchanged input under unchanged code
  is never recomputed, on any machine. Results are immutable: the same key is the same
  result, so two runs never conflict over one.
* **The curation state** (`State`): what a refresh cannot recompute cheaply (the fetch
  index, each input's descriptor, the cards), as named sections of small JSON entries,
  stored in content-addressed chunks so a refresh that changed little uploads little.
  The host points at its root; saving it is a compare-and-swap, done with the version
  commit, under a lease that keeps two refreshes from running at once.
* **Revisions are checked before anything runs** (`check_lock`): a step whose code or
  reader libraries changed since `steps.lock` refuses to run until its author bumps its
  REVISION (it re-runs) or relocks (its output cannot change). Nothing goes stale
  silently, and a comment edit never spends money.
* **Spending is planned** (`Budget`): model calls are counted and priced before any is
  made, and a run over its budget stops before the first one.

`Store` is the host as a refresh sees it; `MemoryStore` is one in memory, for tests.
A curator's adapter (GiveWell's crawl and item rules; a Drive folder sync) defines its
steps and drives a `Curation`; the canopy CLI's `canopy kb refresh` supplies the store.
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import importlib.metadata
import json
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from canopy_kb import records

RESULTS, RECORDS, ORIGINALS = "results", "records", "originals"
#: Buckets per state section: a section's entries are split by key hash, so a refresh
#: that touched a few entries re-uploads a few chunks.
CHUNKS = 64
STATE_FORMAT = 1
#: Objects per request to the host (its limit).
BATCH = 1000
#: Results held before they are sent (gzipped bytes).
PENDING_BYTES = 64 * 1024**2


class CurationError(RuntimeError):
    pass


class StaleStep(CurationError):
    """A step's code changed since steps.lock without its REVISION saying so."""


class OverBudget(CurationError):
    """The model calls a refresh plans cost more than it may spend."""


def sha(obj) -> str:
    return records.sha(obj)


def encode(obj) -> bytes:
    return records.encode(obj)


def decode(data: bytes):
    return json.loads(gzip.decompress(data))


# ---- steps -----------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Step:
    """One kind of derived result.

    `revision` changes when the step's output would; `settings` holds everything else
    that defines it (a prompt template, a model, a reader's page selector) and changes
    the key by itself. `sources` are the files whose code produces it (shared helpers
    included) and `libraries` the packages it reads with: `check_lock` compares them
    with the lock. `cost` is an estimate in USD per call, for a step that calls a model."""

    name: str
    revision: str
    settings: dict = dataclasses.field(default_factory=dict)
    sources: tuple[str, ...] = ()
    libraries: tuple[str, ...] = ()
    cost: float = 0.0

    @property
    def fingerprint(self) -> str:
        return sha({"step": self.name, "revision": self.revision, "settings": self.settings})

    def key(self, **inputs) -> str:
        """The result's address: this step, as it is now, applied to `inputs` (hashes and
        small fields, never bytes)."""
        return sha({"fp": self.fingerprint, "in": inputs})


def source_hash(paths: Iterable[str | Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        h.update(Path(p).name.encode() + b"\0" + Path(p).read_bytes() + b"\0")
    return h.hexdigest()


def _versions(libraries: Iterable[str]) -> dict[str, str]:
    out = {}
    for lib in libraries:
        try:
            out[lib] = importlib.metadata.version(lib)
        except importlib.metadata.PackageNotFoundError:
            out[lib] = "absent"
    return out


def lock_entry(step: Step) -> dict:
    return {"revision": step.revision, "sources": source_hash(step.sources), "libraries": _versions(step.libraries)}


def check_lock(steps: Iterable[Step], lock_path: Path) -> None:
    """Refuse to run code the lock does not describe (see the module docstring)."""
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8")) if Path(lock_path).exists() else {}
    stale = []
    for s in steps:
        now, was = lock_entry(s), lock.get(s.name)
        if was is None:
            stale.append(f"{s.name}: not in {Path(lock_path).name}")
        elif was.get("revision") != now["revision"]:
            stale.append(f"{s.name}: REVISION is {now['revision']}, the lock says {was.get('revision')} (relock)")
        elif was.get("sources") != now["sources"]:
            stale.append(f"{s.name}: its code changed")
        elif was.get("libraries") != now["libraries"]:
            moved = {k: f"{was.get('libraries', {}).get(k)} -> {v}" for k, v in now["libraries"].items()
                     if was.get("libraries", {}).get(k) != v}
            stale.append(f"{s.name}: reader libraries moved {moved}")
    if stale:
        raise StaleStep(
            "the curation code changed since its lock:\n  " + "\n  ".join(stale) + "\n"
            "For each: bump the step's REVISION if its output can change (it re-runs on the next refresh), "
            f"then relock (`write_lock`) to record it; relock alone if it cannot (a comment, a refactor). "
            f"Lock: {lock_path}")


def write_lock(steps: Iterable[Step], lock_path: Path) -> dict:
    lock = {s.name: lock_entry(s) for s in sorted(steps, key=lambda s: s.name)}
    Path(lock_path).write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return lock


# ---- the host ---------------------------------------------------------------------------

class Store(Protocol):
    """The knowledge base's host, as a refresh sees it."""

    def have(self, store: str, keys: list[str]) -> set[str]: ...
    def get(self, store: str, keys: list[str]) -> dict[str, bytes]: ...
    def put_results(self, blobs: dict[str, bytes]) -> int: ...
    def put_records(self, blobs: dict[str, bytes]) -> int: ...
    def put_originals(self, files: dict[str, tuple[bytes, str]]) -> int: ...
    def curation(self) -> dict: ...
    def take_lease(self, holder: str, seconds: int) -> dict: ...
    def drop_lease(self, holder: str) -> None: ...
    def save_curation(self, sha: str, base: str | None) -> dict: ...
    def plan(self, state: dict) -> dict: ...
    def commit(self, state: dict, base: int | None, curation: dict | None) -> dict: ...


class MemoryStore:
    """A host in memory: what `canopy kb refresh` talks to, for tests."""

    def __init__(self):
        self.objects: dict[str, dict[str, bytes]] = {RESULTS: {}, RECORDS: {}, ORIGINALS: {}}
        self.kb_curation: dict = {}
        self.versions: list[dict] = []  # each {number, state, curation}
        self.calls: list[str] = []

    # objects
    def have(self, store, keys):
        self.calls.append(f"have {store} {len(keys)}")
        return {k for k in keys if k in self.objects[store]}

    def get(self, store, keys):
        self.calls.append(f"get {store} {len(keys)}")
        return {k: self.objects[store][k] for k in keys if k in self.objects[store]}

    def _put(self, store, blobs):
        new = {k: v for k, v in blobs.items() if k not in self.objects[store]}
        self.objects[store].update(new)
        self.calls.append(f"put {store} {len(new)}")
        return len(new)

    def put_results(self, blobs):
        return self._put(RESULTS, blobs)

    def put_records(self, blobs):
        return self._put(RECORDS, blobs)

    def put_originals(self, files):
        return self._put(ORIGINALS, {k: v[0] for k, v in files.items()})

    # curation
    def curation(self):
        return dict(self.kb_curation)

    def take_lease(self, holder, seconds):
        lease = self.kb_curation.get("lease")
        if lease and lease["holder"] != holder:
            raise CurationError(f"another refresh ({lease['holder']}) holds the lease")
        self.kb_curation["lease"] = {"holder": holder}
        return self.curation()

    def drop_lease(self, holder):
        if (self.kb_curation.get("lease") or {}).get("holder") == holder:
            self.kb_curation.pop("lease")

    def save_curation(self, sha_, base):
        cur = self.kb_curation.get("sha")
        if cur and cur != sha_ and base != cur:
            raise CurationError("the curation state was saved since by another refresh")
        if sha_ not in self.objects[RESULTS]:
            raise CurationError(f"no curation state {sha_[:12]} uploaded")
        self.kb_curation["sha"] = sha_
        self.kb_curation["version"] = self.versions[-1]["number"] if self.versions else None
        return self.curation()

    # publishing
    def head(self) -> dict:
        return self.versions[-1]["state"] if self.versions else {"docs": {}, "parts": {}}

    def plan(self, state):
        d = records.diff(self.head(), state)
        return {"base": self.versions[-1]["number"] if self.versions else None,
                "unchanged": not (d["added"] or d["changed"] or d["removed"] or d["parts"]),
                "added": len(d["added"]), "changed": len(d["changed"]), "removed": len(d["removed"]),
                "upload_docs": d["added"] + d["changed"], "upload_parts": d["parts"], "full_required": False}

    def commit(self, state, base, curation):
        if (self.versions[-1]["number"] if self.versions else None) != base:
            raise CurationError("the knowledge base moved since the plan")
        d = records.diff(self.head(), state)
        need = {state["docs"][i] for i in d["added"] + d["changed"]} | {state["parts"][n] for n in d["parts"]}
        missing = need - set(self.objects[RECORDS])
        if missing:
            raise CurationError(f"{len(missing)} records were not uploaded")
        for i in d["added"] + d["changed"]:
            rec = decode(self.objects[RECORDS][state["docs"][i]])
            if rec.get("text_sha") and rec["text_sha"] not in self.objects[RECORDS]:
                raise CurationError(f"{i}: its text was not uploaded")
        changed = bool(d["added"] or d["changed"] or d["removed"] or d["parts"])
        cur = self.kb_curation.get("sha")
        if curation and cur and cur != curation["sha"] and curation.get("base") != cur:
            raise CurationError("the curation state was saved since by another refresh")  # and no version
        if changed:
            self.versions.append({"number": len(self.versions) + 1, "state": state, "diff": d})
        if curation:
            self.save_curation(curation["sha"], curation.get("base"))
        return {"created": changed, "version": {"number": self.versions[-1]["number"]} if changed else None}

    # what a test asks of it
    def item(self, iid: str) -> dict:
        """A published document, joined with its text, as the index would store it."""
        rec = decode(self.objects[RECORDS][self.head()["docs"][iid]])
        return records.join(rec, decode(self.objects[RECORDS][rec["text_sha"]])) if "text_sha" in rec else rec


# ---- the curation state -----------------------------------------------------------------

def _bucket(key: str) -> int:
    return int(hashlib.sha1(key.encode()).hexdigest()[:4], 16) % CHUNKS


class State:
    """Named sections of small JSON entries. Read whole at the start of a refresh, saved
    as content-addressed chunks: unchanged chunks keep their address and are not sent."""

    def __init__(self, sections: dict[str, dict] | None = None, root: str | None = None, version: int | None = None):
        self.sections: dict[str, dict] = {k: dict(v) for k, v in (sections or {}).items()}
        self.root = root          # the root it was read from: the save's compare-and-swap base
        self.version = version    # the knowledge-base version it was saved with

    def section(self, name: str) -> dict:
        return self.sections.setdefault(name, {})

    def chunks(self) -> tuple[dict, dict[str, bytes]]:
        """(root, {chunk key: gzipped chunk})."""
        out: dict[str, bytes] = {}
        root: dict[str, Any] = {"format": STATE_FORMAT, "sections": {}}
        for name in sorted(self.sections):
            buckets: list[dict] = [{} for _ in range(CHUNKS)]
            for k, v in self.sections[name].items():
                buckets[_bucket(k)][k] = v
            keys = []
            for n, entries in enumerate(buckets):
                if not entries:
                    keys.append("")
                    continue
                chunk = {"section": name, "bucket": n, "entries": entries}
                ck = sha(chunk)
                out[ck] = encode(chunk)
                keys.append(ck)
            root["sections"][name] = keys
        return root, out

    @classmethod
    def load(cls, store: Store) -> State:
        cur = store.curation()
        root_key = cur.get("sha")
        if not root_key:
            return cls(version=None)
        got = store.get(RESULTS, [root_key])
        if root_key not in got:
            raise CurationError(f"the curation state {root_key[:12]} is not stored")
        root = decode(got[root_key])
        if root.get("format") != STATE_FORMAT:
            raise CurationError(f"curation state format {root.get('format')} (this code reads {STATE_FORMAT})")
        want = [k for keys in root["sections"].values() for k in keys if k]
        blobs = get_all(store, RESULTS, want)
        missing = set(want) - set(blobs)
        if missing:
            raise CurationError(f"{len(missing)} chunks of the curation state are not stored")
        sections: dict[str, dict] = {}
        for name, keys in root["sections"].items():
            sec = sections.setdefault(name, {})
            for k in keys:
                if k:
                    sec.update(decode(blobs[k])["entries"])
        return cls(sections, root=root_key, version=cur.get("version"))

    def upload(self, store: Store) -> str:
        """Store the chunks the host lacks and the root; returns the root's key."""
        root, chunks = self.chunks()
        put_missing(store, chunks)
        rk = sha(root)
        put_missing(store, {rk: encode(root)})
        return rk


def _batches(seq: list, n: int = BATCH):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def get_all(store: Store, kind: str, keys: Iterable[str]) -> dict[str, bytes]:
    keys = sorted(set(keys))
    out: dict[str, bytes] = {}
    for batch in _batches(keys):
        out.update(store.get(kind, batch))
    return out


def put_missing(store: Store, blobs: dict[str, bytes], kind: str = RESULTS) -> int:
    """Upload the `blobs` the host lacks; returns how many were sent."""
    keys = sorted(blobs)
    have: set[str] = set()
    for batch in _batches(keys):
        have |= store.have(kind, batch)
    todo = {k: blobs[k] for k in keys if k not in have}
    if not todo:
        return 0
    put = store.put_results if kind == RESULTS else store.put_records
    n = 0
    for batch in _batches(sorted(todo)):
        n += put({k: todo[k] for k in batch})
    return n


# ---- the budget ---------------------------------------------------------------------------

class Budget:
    """What a refresh may spend on model calls, planned before any is made."""

    def __init__(self, usd: float):
        self.usd = float(usd)
        self.planned: dict[str, dict] = {}

    def plan(self, step: Step, calls: int) -> None:
        self.planned[step.name] = {"calls": int(calls), "usd": round(calls * step.cost, 2)}

    @property
    def total(self) -> float:
        return round(sum(p["usd"] for p in self.planned.values()), 2)

    def check(self) -> None:
        if self.total > self.usd:
            lines = ", ".join(f"{n}: {p['calls']} calls (~${p['usd']})" for n, p in self.planned.items() if p["calls"])
            raise OverBudget(f"this refresh would make {lines}: about ${self.total}, over its budget of ${self.usd}. "
                             f"Nothing was spent. Rerun with --budget {max(1, int(self.total + 1))} to go ahead.")


# ---- a refresh ------------------------------------------------------------------------------

class Curation:
    """One refresh: its lease, the state it started from, the results it reads and writes,
    and its publish. Use as a context manager: the lease is released however it ends."""

    def __init__(self, store: Store, budget: Budget | None = None, holder: str = "", lease_seconds: int = 4 * 3600):
        self.store = store
        self.budget = budget or Budget(0)
        self.holder = holder or f"refresh-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = lease_seconds
        self.state: State = State()
        self._pending: dict[str, bytes] = {}
        self._pending_bytes = 0
        self.report: dict[str, Any] = {"fetched_results": 0, "stored_results": 0}

    def __enter__(self):
        self.store.take_lease(self.holder, self.lease_seconds)
        self.state = State.load(self.store)
        return self

    def __exit__(self, *exc):
        try:
            self.flush()
        finally:
            self.store.drop_lease(self.holder)

    # results
    def results(self, keys: Iterable[str]) -> dict[str, Any]:
        """The stored results among `keys`. Not kept in memory: a full rebuild's texts
        would not fit; a caller keeps what it uses."""
        keys = list(dict.fromkeys(keys))
        out = {k: decode(self._pending[k]) for k in keys if k in self._pending}
        want = [k for k in keys if k not in out]
        if want:
            got = get_all(self.store, RESULTS, want)
            self.report["fetched_results"] += len(got)
            out.update({k: decode(v) for k, v in got.items()})
        return out

    def result(self, key: str):
        return self.results([key]).get(key)

    def put(self, key: str, value, *, now: bool = False) -> None:
        """Store a result (in batches). `now=True` stores it at once: a model result, so a
        run that dies after paying for it never pays again."""
        blob = encode(value)
        self._pending[key] = blob
        self._pending_bytes += len(blob)
        if now or len(self._pending) >= BATCH or self._pending_bytes > PENDING_BYTES:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            pending, self._pending, self._pending_bytes = self._pending, {}, 0
            self.report["stored_results"] += put_missing(self.store, pending)

    def memo(self, key: str, compute: Callable[[], Any], *, now: bool = False):
        """The result under `key`, computed (and stored) only when the host lacks it."""
        got = self.result(key)
        if got is not None:
            return got
        value = compute()
        if value is not None:
            self.put(key, value, now=now)
        return value

    # originals
    def originals(self, shas: Iterable[str]) -> dict[str, bytes]:
        return get_all(self.store, ORIGINALS, shas)

    def put_originals(self, files: dict[str, tuple[bytes, str]]) -> int:
        keys = sorted(files)
        have: set[str] = set()
        for batch in _batches(keys):
            have |= self.store.have(ORIGINALS, batch)
        todo = {k: files[k] for k in keys if k not in have}
        return self.store.put_originals(todo) if todo else 0

    # publishing
    def publish(self, docs: dict[str, dict], text: Callable[[str], str], parts: dict[str, object], *,
                max_removed: float = 0.02, allow_removals: bool = False) -> dict:
        """Publish `docs` ({item id: record, text by reference}) and `parts`, sending only
        what the host lacks; `text(item id)` gives a document's text when it must be sent.
        The curation state goes with the commit, so the two never disagree. A publish that
        would remove more than `max_removed` of the documents stops, unless allowed: a
        source that failed is not a publisher that deleted everything."""
        self.flush()
        state = {"docs": {i: sha(r) for i, r in docs.items()}, "parts": {n: sha(p) for n, p in parts.items()}}
        plan = self.store.plan(state)
        if plan.get("full_required"):
            raise CurationError(plan.get("reason") or "the host asks for a whole-file publish first")
        head_docs = plan["changed"] + plan["removed"] + (len(docs) - plan["added"] - plan["changed"])
        if plan["removed"] and not allow_removals and plan["removed"] > max_removed * max(1, head_docs):
            raise CurationError(f"this refresh would remove {plan['removed']} of {head_docs} documents "
                                f"(more than {max_removed:.0%}): a source probably failed. Nothing was published; "
                                "rerun with --allow-removals if they really are gone.")
        sent = 0
        if not plan["unchanged"]:
            blobs: dict[str, bytes] = {}
            for i in plan["upload_docs"]:
                rec = docs[i]
                blobs[state["docs"][i]] = encode(rec)
                if "text_sha" in rec:
                    blobs.setdefault(rec["text_sha"], encode(text(i)))
            for n in plan["upload_parts"]:
                blobs[state["parts"][n]] = encode(parts[n])
            sent = put_missing(self.store, blobs, RECORDS)
        root = self.state.upload(self.store)
        curation = {"sha": root, "base": self.state.root} if root != self.state.root else None
        if plan["unchanged"]:
            if curation:
                self.store.save_curation(root, self.state.root)
            out = {"published": False, "unchanged": True}
        else:
            c = self.store.commit(state, plan["base"], curation)
            out = {"published": bool(c.get("created")), "version": (c.get("version") or {}).get("number")}
        if curation:
            self.state.root = root
        out.update(added=plan["added"], changed=plan["changed"], removed=plan["removed"], objects_sent=sent,
                   state_saved=bool(curation))
        return out


def record(item: dict) -> dict:
    """An item (text in place) as a record (text by reference): `canopy_kb.records.record`."""
    return records.record(item)
