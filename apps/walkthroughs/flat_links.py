"""Rewrite flat artifact links already STORED in canopy's data (canopy-web#1337).

Owner decision (Jonathan, 2026-10-08): one URL per artifact, `/w/<workspace>/…`,
and nothing forwarded. #1338 (and the storyboard/narrative/release follow-up)
made the flat addresses a plain 404, which broke every flat link already baked
into data canopy serves back: a review's `request_json` (`video.url`,
`deck_url`, a deck deep link in a decision prompt), an agent task's links, and
the HTML decks/docs pages whose `<video src>` points at
`/walkthrough/<id>/content`.

This rewrites those links IN PLACE to the scoped form, using the workspace the
LINKED artifact lives in (a walkthrough id → that walkthrough's workspace, a
share token → its session's, a run id → its run's). It never adds a route or a
redirect. What it keeps exactly: the host (absolute or relative, the labs
`/canopy` mount included), the query string (`?t=<token>`) and the fragment
(`#scene-3`, `#t=18`). What it leaves alone: anything it cannot resolve to
exactly one workspace — prose that names a route (`/review/<id>`), a file path
(`scripts/walkthrough/x.py`), another product's `/share/` page, a deleted
artifact. Those are counted, not guessed.

Idempotent: a scoped link (`/w/<slug>/…`) is not matched, so a second run
changes nothing. Runs from the `walkthroughs/0010` data migration (so the
deploy's migrate step applies it) and from `manage.py rewrite_flat_links`
(`--dry-run` to count first).
"""
from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from collections.abc import Callable

# A link to one of the retired flat addresses, absolute or relative:
#   <host>[/canopy]/walkthrough/<uuid>[/content]   → /w/<ws>/walkthrough/<uuid>…
#   <host>[/canopy]/w/<uuid>[/content]              → /w/<ws>/walkthrough/<uuid>…
#   <host>[/canopy]/review/<uuid>/                  → /w/<ws>/review/<uuid>/
#   <host>[/canopy]/share/<token>                   → /w/<ws>/share/<token>
#   <host>[/canopy]/storyboard/<slug>               → /w/<ws>/storyboard/<slug>
#   <host>[/canopy]/narrative/<slug>?b=<board>      → /w/<ws>/narrative/<slug>?b=…
#   <host>[/canopy]/ddd-release/<narrative>/<run>   → /w/<ws>/ddd-release/…
# Everything after the id (`/content`, `?t=`, `#scene-2`) is left where it is.
#
# The look-behind stops a match starting mid-path (`…/group/3342/share/x`,
# `scripts/walkthrough/x.py`); an absolute URL is matched from its scheme. A
# `/w/<anything>/…` path is consumed whole, so an already-scoped
# `/w/connect/walkthrough/<id>` is never re-matched at its `/walkthrough/`.
FLAT_LINK = re.compile(
    r"(?<![\w.\-/~%])"
    r"(?P<pre>(?:https?://[^/\s\"'<>()\\]+)?(?:/canopy)?)"
    r"/(?:"
    r"w/(?P<w>[^/\s\"'<>?#()\\]+)(?P<wrest>(?:/[^/\s\"'<>?#()\\]*)*)"
    r"|(?P<kind>walkthrough|review|share|storyboard|narrative)/(?P<id>[A-Za-z0-9_-]+)"
    r"|ddd-release/(?P<rn>[A-Za-z0-9_.-]+)/(?P<rr>[A-Za-z0-9_.-]+)"
    r")"
)

# `?…b=<board>` right after a /narrative/<slug> link: the storyboard it is read on.
_BOARD_PARAM = re.compile(r"^\?(?:[^\s\"'<>#]*?&)?b=([A-Za-z0-9_-]+)")

Resolve = Callable[[str, str, str], "str | None"]


def _uuid(s: str) -> str | None:
    try:
        return str(uuid.UUID(s))
    except (ValueError, AttributeError):
        return None


def rewrite_text(text: str, resolve: Resolve) -> tuple[str, int, int]:
    """`text` with every resolvable flat link scoped.

    `resolve(kind, ident, following_text)` returns the workspace slug the linked
    artifact lives in, or None. Returns (new_text, rewritten, unresolved), where
    `unresolved` counts flat-shaped links left as they were.
    """
    if not text or "/" not in text:
        return text, 0, 0
    counts = Counter()

    def sub(m: re.Match) -> str:
        pre = m.group("pre")
        if m.group("w") is not None:
            wid = _uuid(m.group("w"))
            rest = m.group("wrest") or ""
            if wid is None or rest not in ("", "/", "/content"):
                return m.group(0)  # a workspace slug: already scoped
            ws = resolve("walkthrough", wid, "")
            if not ws:
                counts["unresolved"] += 1
                return m.group(0)
            counts["rewritten"] += 1
            return f"{pre}/w/{ws}/walkthrough/{m.group('w')}{'/content' if rest == '/content' else ''}"
        if m.group("rn") is not None:
            ws = resolve("ddd-release", m.group("rr"), "")
            if not ws:
                counts["unresolved"] += 1
                return m.group(0)
            counts["rewritten"] += 1
            return f"{pre}/w/{ws}/ddd-release/{m.group('rn')}/{m.group('rr')}"
        kind, ident = m.group("kind"), m.group("id")
        ws = resolve(kind, ident, m.string[m.end():m.end() + 300])
        if not ws:
            counts["unresolved"] += 1
            return m.group(0)
        counts["rewritten"] += 1
        return f"{pre}/w/{ws}/{kind}/{ident}"

    new = FLAT_LINK.sub(sub, text)
    return new, counts["rewritten"], counts["unresolved"]


def rewrite_json(value, resolve: Resolve) -> tuple[object, int, int]:
    """`rewrite_text` over every string VALUE in a JSON document (keys untouched)."""
    if isinstance(value, str):
        return rewrite_text(value, resolve)
    if isinstance(value, list):
        out, done, left = [], 0, 0
        for item in value:
            v, d, u = rewrite_json(item, resolve)
            out.append(v)
            done, left = done + d, left + u
        return out, done, left
    if isinstance(value, dict):
        out, done, left = {}, 0, 0
        for k, item in value.items():
            v, d, u = rewrite_json(item, resolve)
            out[k] = v
            done, left = done + d, left + u
        return out, done, left
    return value, 0, 0


class DbResolver:
    """Resolve a linked artifact to its workspace from the database.

    Takes an app registry (`django.apps.apps`, or a migration's historical
    `apps`), so the same code runs in the data migration and the command. Only
    an answer that names exactly ONE workspace counts; anything ambiguous or
    missing is None.
    """

    def __init__(self, apps):
        self.apps = apps
        self._cache: dict[tuple[str, str], str | None] = {}

    def __call__(self, kind: str, ident: str, following: str) -> str | None:
        if kind == "narrative":
            board = _BOARD_PARAM.match(following or "")
            if not board:
                return None
            kind, ident = "storyboard", board.group(1)
        key = (kind, ident)
        if key not in self._cache:
            self._cache[key] = self._lookup(kind, ident)
        return self._cache[key]

    def _model(self, app: str, name: str):
        return self.apps.get_model(app, name)

    @staticmethod
    def _one(values) -> str | None:
        found = {v for v in values if v}
        return found.pop() if len(found) == 1 else None

    def _lookup(self, kind: str, ident: str) -> str | None:
        if kind == "walkthrough":
            wid = _uuid(ident)
            if not wid:
                return None
            return self._one(
                self._model("walkthroughs", "Walkthrough").objects.filter(pk=wid)
                .values_list("workspace_id", flat=True)
            )
        if kind == "review":
            rid = _uuid(ident)
            if not rid:
                return None
            return self._one(
                self._model("reviews", "ReviewRequest").objects.filter(pk=rid)
                .values_list("workspace_id", flat=True)
            )
        if kind == "share":
            session = self._one(
                self._model("shared_sessions", "ShareToken").objects.filter(token=ident)
                .values_list("session__workspace_id", flat=True)
            )
            if session:
                return session
            return self._one(
                self._model("shared_sessions", "ArcShareToken").objects.filter(token=ident)
                .values_list("arc__workspace_id", flat=True)
            )
        if kind == "storyboard":
            return self._one(
                self._model("storyboards", "Storyboard").objects.filter(slug=ident)
                .values_list("workspace_id", flat=True)
            )
        if kind == "ddd-release":
            wts = self._model("walkthroughs", "Walkthrough").objects.filter(run_id=ident)
            revs = self._model("reviews", "ReviewRequest").objects.filter(run_id=ident)
            return self._one(
                [*wts.values_list("workspace_id", flat=True),
                 *revs.values_list("workspace_id", flat=True)]
            )
        return None


# The stored fields that hold links canopy serves back. JSON fields are walked;
# text fields are rewritten as text. A field a model does not have (yet, or any
# more) is skipped.
TARGETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("reviews", "ReviewRequest", ("request_json", "response_json", "suggestions_json")),
    ("walkthroughs", "Walkthrough", ("links", "description")),
    ("agents", "AgentTask", ("links", "notes", "source_url", "ask_body", "plan", "rationale", "review")),
    ("agents", "AgentProject", ("links", "notes")),
    ("storyboards", "Storyboard", ("lede",)),
    ("storyboards", "Act", ("prose",)),
    ("storyboards", "Entry", ("blurb",)),
    ("shareouts", "Shareout", ("links",)),
)


def _field_names(model) -> set[str]:
    return {f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)}


def sweep_rows(apps, resolve: Resolve, *, dry_run: bool, stats: Counter) -> None:
    """Rewrite every TARGETS field. `.update()`, not `.save()`: no signal, no
    `updated_at` bump — the content did not change, only its address."""
    for app_label, model_name, wanted in TARGETS:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        fields = [f for f in wanted if f in _field_names(model)]
        if not fields:
            continue
        for row in model.objects.only("pk", *fields).iterator():
            changes = {}
            for f in fields:
                value = getattr(row, f)
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, str):
                    new, done, left = rewrite_text(value, resolve)
                else:
                    new, done, left = rewrite_json(value, resolve)
                stats["links_unresolved"] += left
                if done:
                    stats["links_rewritten"] += done
                    stats[f"{model_name}.{f}"] += done
                    changes[f] = new
            if changes:
                stats["rows_rewritten"] += 1
                if not dry_run:
                    model.objects.filter(pk=row.pk).update(**changes)


def sweep_blobs(apps, resolve: Resolve, *, dry_run: bool, stats: Counter,
                budget_s: float = 360.0, client=None) -> None:
    """Rewrite the links inside stored HTML walkthroughs (decks, docs pages).

    The bytes live in Drive, so a rewritten page is uploaded as a NEW file in the
    walkthrough's own folder, the row is pointed at it, and the old file is
    trashed (recoverable for 30 days). A new file id, not an in-place replace,
    because the content route serves these as immutable and keys its ETag on
    the file id. Bounded by `budget_s` so a slow Drive cannot stall a deploy's
    migrate step; whatever it did not reach is counted and can be finished with
    `manage.py rewrite_flat_links`.
    """
    from apps.walkthroughs.drive_client import DriveNotConfigured, get_drive_client

    Walkthrough = apps.get_model("walkthroughs", "Walkthrough")
    rows = list(
        Walkthrough.objects.filter(content_type__istartswith="text/html")
        .exclude(drive_file_id="")
        .only("pk", "drive_file_id", "drive_folder_id", "content_type", "size_bytes")
        .order_by("created_at")
    )
    if not rows:
        return
    if client is None:
        try:
            client = get_drive_client()
        except DriveNotConfigured as exc:
            stats["blobs_skipped_no_drive"] = len(rows)
            stats["blob_note"] = str(exc)  # type: ignore[assignment]
            return
    deadline = time.monotonic() + budget_s
    for i, w in enumerate(rows):
        if time.monotonic() > deadline:
            stats["blobs_not_reached"] += len(rows) - i
            return
        try:
            data, _, _, _ = client.download(w.drive_file_id)
        except Exception:  # noqa: BLE001 — one unreadable file must not stop the rest
            stats["blobs_failed"] += 1
            continue
        stats["blobs_scanned"] += 1
        html = data.decode("utf-8", errors="surrogateescape")
        new, done, left = rewrite_text(html, resolve)
        stats["blob_links_unresolved"] += left
        if not done:
            continue
        stats["blobs_rewritten"] += 1
        stats["blob_links_rewritten"] += done
        if dry_run:
            continue
        body = new.encode("utf-8", errors="surrogateescape")
        try:
            new_id = client.upload(
                parent_id=w.drive_folder_id, name="index.html",
                content_type=w.content_type, data=body,
            )
        except Exception:  # noqa: BLE001
            stats["blobs_failed"] += 1
            stats["blobs_rewritten"] -= 1
            continue
        old_id = w.drive_file_id
        Walkthrough.objects.filter(pk=w.pk).update(drive_file_id=new_id, size_bytes=len(body))
        try:
            client.delete(old_id)
        except Exception:  # noqa: BLE001 — the row already points at the new file
            stats["blobs_old_not_trashed"] += 1


def sweep(apps, *, dry_run: bool = False, blobs: bool = True, budget_s: float = 360.0,
          client=None) -> Counter:
    """Rewrite stored flat links everywhere. Returns the counts."""
    stats: Counter = Counter()
    resolve = DbResolver(apps)
    sweep_rows(apps, resolve, dry_run=dry_run, stats=stats)
    if blobs:
        sweep_blobs(apps, resolve, dry_run=dry_run, stats=stats, budget_s=budget_s, client=client)
    return stats


def summary(stats: Counter, *, dry_run: bool) -> str:
    head = "flat-links (dry run)" if dry_run else "flat-links"
    return head + ": " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) if stats else head + ": nothing to do"
