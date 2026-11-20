"""A dry run of the brain over the sessions a person can already read (`GET /api/brain/preview`).

For each session of a window the caller reads through legs 1 to 6 (never the brain's own project leg), it says what
the brain WOULD do, and stores nothing: who is in it, whether each class could learn from it now and if everyone in
it agreed (`eligibility.explain`, the judgement `eligible` is), the project links the automatic layers would make
(`associate.propose`, what `associate` writes), and the session as a learner would read it (`transcript_read.read`,
scrubbed). No `BrainSource` is made for a session without one: the compute reads the session itself. No read is
recorded, no event, no cache. `tests/test_brain_preview.py` holds the whole call to no write at all.

`summarise` is the `/brain` page's: the same rows without a line of any transcript, what a page adds up to and its
links grouped, so the page holds no copy of a verdict or a grouping."""
from __future__ import annotations

import datetime as dt
import uuid

from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.canopy_sessions import access, transcript_read
from apps.common import redact

from . import associate, participants, sources
from .eligibility import explain
from .models import BRAIN_RUN_KEY, INDIVIDUAL, PROJECT, SYSTEM, SourceLink

#: Sessions one page answers at most, and characters of one session's transcript.
MAX_LIMIT, MAX_CHARS = 25, 500_000


def window(user, slugs, *, since: dt.datetime, until: dt.datetime, after: str = "", session: str = ""):
    """The sessions `user` reads through legs 1 to 6 in `slugs`, created or active in `[since, until)`, never a
    learning run's own conversation, in id order past `after` (only `session`, when named)."""
    rows = transcript_read.with_activity(
        access.readable_sessions(user, workspace_slugs=slugs, brain=False)
        .exclude(metadata__has_key=BRAIN_RUN_KEY))
    rows = rows.filter(Q(created_at__gte=since, created_at__lt=until) | Q(_activity__gte=since, _activity__lt=until))
    if after:
        rows = rows.filter(id__gt=uuid.UUID(after))
    if session:
        rows = rows.filter(id=uuid.UUID(session))
    return rows.select_related("workspace", "runner_binding", "runner_binding__runner").order_by("id")


def _verdict(e) -> dict:
    return {"ok": e.ok, "reason": e.reason}


def _both(source, session, cls, subject=None) -> dict:
    return {"now": _verdict(explain(source, session=session, cls=cls, subject=subject)),
            "if_everyone_agreed": _verdict(explain(source, session=session, cls=cls, subject=subject,
                                                   assume_consent=True))}


def _people(session) -> list[dict]:
    from apps.contacts.models import Contact  # noqa: PLC0415 — as the other lazy imports here

    found = participants.people_of(session)
    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=[u for _h, u, _c in found if u is not None])}
    contacts = {c.pk: c for c in Contact.objects.filter(pk__in=[c for _h, _u, c in found if c is not None])}
    out = []
    for _how, uid, cid in found:
        if uid is not None:
            u = users.get(uid)
            name = (u.get_full_name() or u.email or u.username) if u is not None else ""
        else:
            c = contacts.get(cid)
            name = (c.display_name or c.email) if c is not None else ""
        out.append({"user_id": uid, "contact_id": cid, "name": name})
    return out


def _links(source, session) -> list[dict]:
    """What the automatic layers would make, beside what is stored that would keep its place."""
    if participants.sealed_reason(session):
        return []
    proposals = [p for p in associate.propose(session, session.workspace_id) if p.tier]
    stored = {}
    if source is not None:
        stored = {row.subject: row for row in SourceLink.objects.filter(
            source=source, subject_type=SourceLink.PROJECT_SUBJECT, subject__in=[str(p.project.pk) for p in proposals])}
    out = []
    for p in proposals:
        row = stored.get(str(p.project.pk))
        new = SourceLink(how=p.how, tier=p.tier)
        kept = row if associate.outranks(row, new) else None
        out.append({"project": p.project.handle, "tier": p.tier, "how": p.how, "evidence": list(p.evidence),
                    "outranked_by": {"how": kept.how, "tier": kept.tier} if kept is not None else None})
    return out


def describe(session, *, view: str, max_chars: int = MAX_CHARS, transcript: bool = True) -> dict:
    """One session, as the brain would see it. Reads only. Without `transcript`, not a line of it is read."""
    source = sources.existing(session)
    people = _people(session)
    would = {PROJECT: _both(source, session, PROJECT), SYSTEM: _both(source, session, SYSTEM),
             INDIVIDUAL: {str(p["user_id"]): _both(source, session, INDIVIDUAL, p["user_id"])
                          for p in people if p["user_id"] is not None}}
    out = {"id": str(session.pk), "title": redact.redact_text(session.title or "")[0],
           "workspace": {"slug": session.workspace_id, "name": session.workspace.display_name},
           "started_at": session.created_at, "participants": people, "would_read": would,
           "links": _links(source, session)}
    if transcript:
        got = transcript_read.read(session, view=view, scrub=True, max_chars=max_chars)
        out["transcript"] = {"view": got.view, "messages": got.messages, "truncated": got.truncated,
                             "chars": got.chars}
    return out


def preview(user, slugs, *, since: dt.datetime, until: dt.datetime, view: str = transcript_read.FULL,
            after: str = "", limit: int = 10, max_chars: int = MAX_CHARS, session: str = "") -> dict:
    """One page of the preview. `next_after` is what to pass as `after`; `done` says nothing is left."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    page = list(window(user, slugs, since=since, until=until, after=after, session=session)[:limit + 1])
    more = len(page) > limit
    page = page[:limit]
    return {"window": {"since": since, "until": until}, "view": view,
            "sessions": [describe(s, view=view, max_chars=max_chars) for s in page],
            "next_after": str(page[-1].pk) if page else after, "done": not more}



def counts(rows: list[dict]) -> dict:
    """What one page of the summary adds up to: sessions each class could read now and if everyone agreed (your own
    `individual` only), sealed ones by why, and the links canopy would store (not kept out by a stored one)."""
    out = {"sessions": len(rows), "links": 0, "sealed": {}}
    for cls in (PROJECT, SYSTEM, INDIVIDUAL):
        out[cls] = {"now": 0, "if_everyone_agreed": 0}
    for row in rows:
        for cls in (PROJECT, SYSTEM, INDIVIDUAL):
            pair = row["you"] if cls == INDIVIDUAL else row["would_read"][cls]
            for when in ("now", "if_everyone_agreed"):
                out[cls][when] += int(bool(pair and pair[when]["ok"]))
        why = row["would_read"][PROJECT]["now"]["reason"]
        if why.startswith("sealed:"):
            out["sealed"][why[len("sealed:"):]] = out["sealed"].get(why[len("sealed:"):], 0) + 1
        out["links"] += sum(1 for lnk in row["links"] if lnk["outranked_by"] is None)
    return out


def would_link(rows: list[dict]) -> list[dict]:
    """The links of a page, grouped by project and tier, each with its sessions and evidence."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        for lnk in row["links"]:
            ws = row["workspace"]["slug"]
            g = groups.setdefault((ws, lnk["project"], lnk["tier"]),
                                  {"workspace": ws, "project": lnk["project"], "tier": lnk["tier"], "sessions": []})
            g["sessions"].append({"id": row["id"], "title": row["title"], "how": lnk["how"],
                                  "evidence": lnk["evidence"], "outranked_by": lnk["outranked_by"]})
    tiers = (SourceLink.CORE, SourceLink.ADJACENT, SourceLink.CANDIDATE, SourceLink.REJECTED)
    rank = {t: i for i, t in enumerate(tiers)}
    return sorted(groups.values(), key=lambda g: (g["workspace"], g["project"], rank.get(g["tier"], 9)))


def summarise(user, slugs, *, since: dt.datetime, until: dt.datetime, after: str = "", limit: int = 10) -> dict:
    """One page of the preview without a line of any transcript (`describe(transcript=False)`), with what the page
    adds up to (`counts`), its links grouped (`would_link`) and how many sessions the whole window holds (`total`).
    The `/brain` page's Preview tab; a transcript is `preview(session=…)`, asked for on a click."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    total = window(user, slugs, since=since, until=until).order_by().count()
    page = list(window(user, slugs, since=since, until=until, after=after)[:limit + 1])
    more = len(page) > limit
    page = page[:limit]
    described = [describe(s, view=transcript_read.FULL, transcript=False) for s in page]
    for row in described:
        row["you"] = row["would_read"][INDIVIDUAL].get(str(user.pk))
    return {"window": {"since": since, "until": until}, "total": total,"sessions": described,
            "counts": counts(described), "would_link": would_link(described),
            "next_after": str(page[-1].pk) if page else after, "done": not more}
