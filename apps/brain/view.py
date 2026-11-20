"""THE data source of every brain read a screen shows: `live` (the default) or `preview:<id>`, a snapshot of what the brain
WOULD do (`snapshots.py`). The same routes, the same response shapes, plus `source` (`out`).

Every surface asks here: the /brain page (`state`, `link_page`, `run_page`, `would_read`), outputs (`outputs`), a
session's Learning… menu (`session_learning`), a project's sessions (`project_sessions`), and `links`, the link read a
graph takes (`graph.brain_reads` can pass its `source` to it).

* `live` is what each surface read before, under its rule, unchanged.
* `preview:<id>` is opened only by its owner, as a person, refused as the preview refuses (`api_preview.the_person`),
  else not found (`resolve`). Its rows are cut, on every read, to the sessions the owner still reads through legs 1 to
  6 and nobody has removed (`snapshots.removed_sessions`), and to projects the owner may read: a snapshot never shows
  more than its owner could see now. It holds no runs and no removal work.

Nothing here writes, and no decision (a learner's read, `eligible`, the cascade) reads a snapshot."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from django.conf import settings

from apps.agents.models import AgentProject
from apps.agents.services.projects import may_read_project
from apps.canopy_sessions import access
from apps.common import redact

from . import outputs as outputs_mod
from . import preview, snapshots, state
from .models import BrainPreviewSnapshot, SourceExclusion, SourceLink

LIVE, PREVIEW = "live", "preview"


@dataclass(frozen=True)
class Source:
    kind: str = LIVE
    snapshot: BrainPreviewSnapshot | None = None

    @property
    def live(self) -> bool:
        return self.snapshot is None


LIVE_SOURCE = Source()


class NotFound(LookupError):
    """No such preview for this person (or it expired)."""


class Invalid(ValueError):
    """A `source` that is neither `live` nor `preview:<id>`."""


def parse(text: str) -> tuple[str, str]:
    text = (text or LIVE).strip()
    if text == LIVE:
        return LIVE, ""
    kind, _, ident = text.partition(":")
    if kind != PREVIEW or not ident.isdecimal():
        raise Invalid("source is `live` or `preview:<id>`")
    return PREVIEW, ident


def resolve(request, text: str) -> Source:
    """The source a request names. A preview is its owner's alone, as a person: anyone else, an agent, a turn's token
    or an MCP call is refused as the preview refuses (403), and a preview that is not theirs is not found."""
    kind, ident = parse(text)
    if kind == LIVE:
        return LIVE_SOURCE
    from .api_preview import the_person  # noqa: PLC0415 — the routes import this module

    snap = snapshots.live_for(the_person(request), ident)
    if snap is None:
        raise NotFound(ident)
    return Source(PREVIEW, snap)


def out(src: Source) -> dict:
    if src.live:
        return {"kind": LIVE}
    s = src.snapshot
    return {"kind": PREVIEW, "id": s.pk, "window": {"since": s.since, "until": s.until}, "created_at": s.created_at}


# ---- what a snapshot may show its owner now ----------------------------------------------------------------

def visible_sessions(user, snap: BrainPreviewSnapshot) -> set[str]:
    """The snapshot's sessions `user` still reads (legs 1 to 6) that nobody has removed."""
    ids = list(snap.session_ids or [])
    readable = {str(i) for i in access.readable_ids(user, [uuid.UUID(i) for i in ids])}
    return readable - snapshots.removed_sessions(readable)


def _rows(user, snap) -> list[dict]:
    ok = visible_sessions(user, snap)
    return [r for r in snap.payload.get("sessions", []) if r["id"] in ok]


def _links_of(user, snap, ok: set[str] | None = None) -> list[dict]:
    ok = visible_sessions(user, snap) if ok is None else ok
    rows = [x for x in snap.payload.get("links", []) if x["session_id"] in ok]
    projects = snapshots.readable_projects(user, {x["project_id"] for x in rows})
    return [x for x in rows if x["project_id"] in projects]


# ---- the link read ---------------------------------------------------------------------------------------

def links(viewer, session_ids, *, source: Source = LIVE_SOURCE) -> list[dict]:
    """Project links of `session_ids`, from `source`: `{session_id, subject (the project's pk), tier, how, confidence,
    evidence, by_id, updated_at}`. Live: the stored rows. A preview: the effective links its snapshot computed, cut to
    what its owner reads now. Every tier: the caller applies its own rule about which it shows."""
    ids = {str(i) for i in session_ids}
    if source.live:
        return [{"session_id": str(x.pop("source__session_id")), **x} for x in SourceLink.objects.filter(
            source__session_id__in=list(ids), subject_type=SourceLink.PROJECT_SUBJECT).values(
            "source__session_id", "subject", "tier", "how", "confidence", "evidence", "by_id", "updated_at")]
    return [{"session_id": x["session_id"], "subject": x["project_id"], "tier": x["tier"], "how": x["how"],
             "confidence": x["confidence"], "evidence": x["evidence"], "by_id": x["by_id"],
             "updated_at": source.snapshot.created_at}
            for x in _links_of(viewer, source.snapshot) if x["session_id"] in ids]


# ---- the /brain page ---------------------------------------------------------------------------------------

def state_of(user, slugs, src: Source) -> dict:
    if src.live:
        return {**state.summary(user, slugs), "source": out(src)}
    snap = src.snapshot
    rows = _rows(user, snap)
    ok = {r["id"] for r in rows}
    per_ws: dict[str, dict] = {}
    for r in rows:
        ws = r["workspace"]["slug"]
        per_ws.setdefault(ws, {"workspace": ws, "scope": "yours", "open": 0, "withdrawn": 0, "sealed": {}})["open"] += 1
    groups: dict[tuple, dict] = {}
    for x in _links_of(user, snap, ok):
        g = groups.setdefault((x["workspace"], x["project"], x["tier"]), {
            "project": x["project"], "name": x["name"], "workspace": x["workspace"], "tier": x["tier"], "count": 0})
        g["count"] += 1
    kept = snap.payload.get("state", {})
    return {"reads_enabled": bool(getattr(settings, "BRAIN_READS_ENABLED", False)), "workspaces": sorted(set(slugs)),
            "admin_workspaces": [], "canopy_admin": bool(getattr(user, "is_superuser", False)),
            "consent": kept.get("consent", {"mine": [], "workspaces": []}),
            "sources": [per_ws[k] for k in sorted(per_ws)], "links": [groups[k] for k in sorted(groups)],
            "learners": kept.get("learners", []), "runs": {"open": 0, "done": 0, "failed": 0}, "cascade": [],
            "source": out(src)}


def link_page(user, slugs, src: Source, *, before: int = 0, limit: int = 50) -> dict:
    if src.live:
        return {**state.link_page(user, slugs, before=before, limit=limit), "source": out(src)}
    rows = {r["id"]: r for r in _rows(user, src.snapshot)}
    found = sorted(_links_of(user, src.snapshot, set(rows)),
                   key=lambda x: (rows[x["session_id"]]["started_at"], x["project"]), reverse=True)
    numbered = [(len(found) - i, x) for i, x in enumerate(found)]
    if before:
        numbered = [(n, x) for n, x in numbered if n < before]
    limit = max(1, min(int(limit), state.MAX_LIMIT))
    page = numbered[:limit]
    return {"links": [{"id": n, "project": x["project"], "name": x["name"], "workspace": x["workspace"],
                       "tier": x["tier"], "how": x["how"], "confidence": x["confidence"], "evidence": x["evidence"],
                       "session_id": x["session_id"], "title": rows[x["session_id"]]["title"],
                       "started_at": rows[x["session_id"]]["started_at"]} for n, x in page],
            "next_before": page[-1][0] if page else 0, "done": len(numbered) <= limit, "source": out(src)}


def run_page(user, slugs, src: Source, *, before: int = 0, limit: int = 25) -> dict:
    if src.live:
        return {**state.run_page(user, slugs, before=before, limit=limit), "source": out(src)}
    return {"runs": [], "next_before": 0, "done": True, "source": out(src)}


def would_read(user, slugs, src: Source, *, since: dt.datetime, until: dt.datetime, after: str = "",
               limit: int = 10) -> dict:
    """Per session: who is in it, whether each class could read it now and if everyone agreed, and the links canopy
    would make. Live: computed now over `[since, until)`. A preview: what its snapshot computed over its window."""
    if src.live:
        return {**preview.summarise(user, slugs, since=since, until=until, after=after, limit=limit),
                "source": out(src)}
    snap = src.snapshot
    rows = sorted(_rows(user, snap), key=lambda r: r["id"])
    total = len(rows)
    if after:
        rows = [r for r in rows if r["id"] > after]
    limit = max(1, min(int(limit), preview.MAX_LIMIT))
    page = rows[:limit]
    return {"window": {"since": snap.since, "until": snap.until}, "total": total, "sessions": page,
            "counts": preview.counts(page), "would_link": preview.would_link(page),
            "next_after": page[-1]["id"] if page else after, "done": len(rows) <= limit, "source": out(src)}


# ---- outputs ---------------------------------------------------------------------------------------------

def outputs(user, src: Source, *, as_person: bool, subject_type: str = "", subject: str = "",
            limit: int = 100) -> list[dict]:
    """What learning produced that `user` may read (`outputs.visible_to`), or a preview's recommendations: would-be
    `system` outputs, each shown only while every session it cites is one its owner still reads."""
    limit = max(1, min(int(limit), 500))
    if src.live:
        qs = (outputs_mod.visible_to(user, as_person=as_person).select_related("capability")
              .order_by("-created_at", "-pk"))
        if subject_type:
            qs = qs.filter(subject_type=subject_type)
        if subject:
            qs = qs.filter(subject=subject)
        return [{"id": o.pk, "learner": o.capability.slug, "kind": o.kind, "workspace": o.workspace_id,
                 "subject_type": o.subject_type, "subject": o.subject, "title": o.title, "body": o.body,
                 "state": o.state, "created_at": o.created_at, "source": out(src)} for o in qs[:limit]]
    ok = visible_sessions(user, src.snapshot)
    rows = [o for o in src.snapshot.outputs or [] if set(o["sessions"]) <= ok]
    rows = [o for o in rows if (not subject_type or subject_type == "system") and (not subject or subject == "")]
    return [{"id": o["id"], "learner": o["learner"], "kind": o["kind"], "workspace": o["workspace"],
             "subject_type": "system", "subject": "", "title": o["title"],
             "body": o["body"], "state": "active", "created_at": o["created_at"], "sessions": o["sessions"],
             "source": out(src)} for o in rows][:limit]


# ---- one session, one project ----------------------------------------------------------------------------

def session_learning(user, session, src: Source) -> dict | None:
    """Whether `user` took `session` out of learning, and the projects it is tied to that they may read (only if they
    read it now). A preview: the ties its snapshot would make, or None when the session is not in it."""
    from . import sources  # noqa: PLC0415 — as the other lazy imports here

    if not src.live:
        if str(session.pk) not in visible_sessions(user, src.snapshot):
            return None
        mine = [x for x in _links_of(user, src.snapshot) if x["session_id"] == str(session.pk)]
        return {"session_id": session.pk, "excluded": False, "source": out(src),
                "projects": [{"project": x["project"], "name": x["name"], "tier": x["tier"], "how": x["how"]}
                             for x in mine]}
    source = sources.existing(session)
    excluded = source is not None and SourceExclusion.objects.filter(user=user, source=source).exists()
    found = []
    if source is not None and access.readable_sessions(user).filter(pk=session.pk).exists():
        for link in source.links.filter(subject_type=SourceLink.PROJECT_SUBJECT).order_by("pk"):
            project = AgentProject.objects.filter(pk=link.subject).first()
            if project is not None and may_read_project(user, project):   # never reveal a project they cannot read
                found.append({"project": project.handle, "name": project.name, "tier": link.tier, "how": link.how})
    return {"session_id": session.pk, "excluded": excluded, "projects": found, "source": out(src)}


def project_sessions(user, project: AgentProject, src: Source, *, tiers, limit: int = 200) -> list[dict]:
    """The sessions tied to `project` (in `tiers`) that `user` may read: ids, titles, how each tie was made."""
    limit = max(1, min(limit, 500))
    if not src.live:
        ok = visible_sessions(user, src.snapshot)
        rows = {r["id"]: r for r in src.snapshot.payload.get("sessions", []) if r["id"] in ok}
        mine = [x for x in _links_of(user, src.snapshot, ok)
                if x["project_id"] == str(project.pk) and x["tier"] in tiers]
        mine.sort(key=lambda x: rows[x["session_id"]]["started_at"], reverse=True)
        return [{"session_id": x["session_id"], "title": rows[x["session_id"]]["title"], "workspace": x["workspace"],
                 "tier": x["tier"], "how": x["how"], "confidence": x["confidence"],
                 "started_at": rows[x["session_id"]]["started_at"], "source": out(src)} for x in mine[:limit]]
    found = (SourceLink.objects.filter(subject_type=SourceLink.PROJECT_SUBJECT, subject=str(project.pk),
                                       tier__in=list(tiers), source__session__isnull=False)
             .select_related("source__session").order_by("-source__session__created_at"))
    readable = access.readable_ids(user, [lnk.source.session_id for lnk in found[:2000]], brain=True)
    rows = []
    for link in found[:2000]:
        s = link.source.session
        if s.pk in readable:
            rows.append({"session_id": s.pk, "title": redact.redact_text(s.title or "")[0], "workspace": s.workspace_id,
                         "tier": link.tier, "how": link.how, "confidence": link.confidence, "started_at": s.created_at,
                         "source": out(src)})
        if len(rows) >= limit:
            break
    return rows
