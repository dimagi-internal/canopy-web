"""Preview snapshots (`BrainPreviewSnapshot`): what the brain WOULD do over a person's sessions, kept so the same screens
can show it (`view.py`, a `preview:<id>` source).

* **Made by the live code.** `create` asks what the preview asks (`preview.window`, `preview.describe` over
  `eligibility.explain` and `associate.propose`) and what the /brain page asks (`state.summary`), and writes ONE row:
  no source, link, output, run or read. `tests/test_brain_previews.py` captures the call's queries.
* **Effective links.** Per session and project, what the session would be tied to once the automatic layers ran: the
  layer's proposal, unless a stored link keeps its place (`associate.outranks`, as `preview._links` says), and every
  stored link no proposal touches.
* **Recommendations** (`put_outputs`) are made on the owner's machine by `/canopy:brain-preview --publish <id>`: would-be
  `system` outputs, each citing sessions of the snapshot the owner still reads, scrubbed like `brain_emit`'s.
* **Removal reaches it.** A session whose words are gone, that is sealed, withdrawn or that anyone took out of learning
  (`removed_sessions`, the shared-use rule) leaves every snapshot at the next cascade sweep (`sweep`, registered with
  `cascade.register_store`), with every recommendation citing it; every read drops one already removed, and one its
  owner no longer reads (`view.py`). A snapshot expires after `TTL` (`expire`, the `brain.previews` tick).
* **A fence.** Nothing that decides anything (a learner's read, `eligible`, the cascade's judgement) reads a snapshot."""
from __future__ import annotations

import datetime as dt
import json
import uuid

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.utils import timezone

from apps.agents.models import AgentProject
from apps.agents.services.projects import project_read_q
from apps.canopy_sessions.models import Session
from apps.common import redact

from . import participants, preview, state
from .emit import _scrub
from .models import INDIVIDUAL, BrainPreviewSnapshot, BrainSource, SourceExclusion, SourceLink

#: How long a snapshot is kept.
TTL = dt.timedelta(days=7)
#: Sessions one snapshot holds at most; past it the snapshot says `truncated`.
MAX_SESSIONS = 200
#: Recommendations one snapshot holds, and sessions one cites.
MAX_OUTPUTS, MAX_CITED = 100, 50
#: The learner a recommendation is said to come from, unless it names another.
DEFAULT_LEARNER = "system-learning"
KIND = "brain.preview"


class RefusedError(ValueError):
    """A request the snapshot will not take; the message says why."""


def _json(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def removed_sessions(session_ids) -> set[str]:
    """Which of `session_ids` (as text) no snapshot may show: gone, words deleted, sealed (`participants.sealed_reason`),
    a source withdrawn, or taken out of learning by anyone (shared use, as `eligible`). Three reads, live."""
    ids = {str(i) for i in session_ids if i}
    if not ids:
        return set()
    found = {str(s.pk): s for s in Session.objects.filter(pk__in=list(ids))
             .only("pk", "metadata", "contact_id", "content_deleted_at")}
    out = {i for i in ids if i not in found or participants.sealed_reason(found[i])}
    sources = BrainSource.objects.filter(session_id__in=list(found))
    out |= {str(s) for s in sources.filter(state=BrainSource.WITHDRAWN).values_list("session_id", flat=True)}
    out |= {str(s) for s in SourceExclusion.objects.filter(source__in=sources)
            .values_list("source__session_id", flat=True)}
    return out


def _effective_links(owner, sessions, described) -> list[dict]:
    """Per session and project, the tie it would have once the automatic layers ran (module docstring)."""
    by_id = {str(s.pk): s for s in sessions}
    handles = {(by_id[r["id"]].workspace_id, lnk["project"]) for r in described for lnk in r["links"]}
    q = Q(pk__in=[])
    for ws, handle in handles:
        q |= Q(workspace_id=ws, handle=handle)
    readable = AgentProject.objects.filter(project_read_q(owner))
    projects = {(p.workspace_id, p.handle): p for p in readable.filter(q)}
    stored = list(SourceLink.objects.filter(source__session_id__in=list(by_id), subject_type=SourceLink.PROJECT_SUBJECT)
                  .values("source__session_id", "subject", "tier", "how", "confidence", "evidence", "by_id"))
    projects.update({(p.workspace_id, p.handle): p for p in readable.filter(
        pk__in=[int(x["subject"]) for x in stored if str(x["subject"]).isdecimal()])})
    by_pk = {str(p.pk): p for p in projects.values()}
    out: dict[tuple, dict] = {}

    def put(session_id, p, **row):
        out[(session_id, str(p.pk))] = {"session_id": session_id, "project_id": str(p.pk), "project": p.handle,
                                         "name": p.name, "workspace": p.workspace_id, **row}

    for x in stored:
        p = by_pk.get(str(x["subject"]))
        if p is not None:
            put(str(x["source__session_id"]), p, tier=x["tier"], how=x["how"], confidence=x["confidence"],
                evidence=[str(e) for e in x["evidence"] or []], by_id=x["by_id"], new=False)
    for r in described:
        for lnk in r["links"]:
            p = projects.get((by_id[r["id"]].workspace_id, lnk["project"]))
            if p is None or lnk["outranked_by"] is not None:
                continue                                   # a stored link keeps its place: already put above
            put(r["id"], p, tier=lnk["tier"], how=lnk["how"], confidence=None, evidence=list(lnk["evidence"]),
                by_id=None, new=True)
    return list(out.values())


def create(owner, slugs, *, since: dt.datetime, until: dt.datetime, view: str = "full") -> BrainPreviewSnapshot:
    """Compute and keep one snapshot. Writes that row and nothing else."""
    page = list(preview.window(owner, slugs, since=since, until=until)[:MAX_SESSIONS + 1])
    truncated = len(page) > MAX_SESSIONS
    page = page[:MAX_SESSIONS]
    removed = removed_sessions([s.pk for s in page])
    kept = [s for s in page if str(s.pk) not in removed]
    described = [preview.describe(s, view=view, transcript=False) for s in kept]
    for row in described:
        row["you"] = row["would_read"][INDIVIDUAL].get(str(owner.pk))
    now = state.summary(owner, slugs)
    payload = {"sessions": described, "links": _effective_links(owner, kept, described), "left_out": len(removed),
               "state": {"consent": now["consent"], "learners": now["learners"]}}
    return BrainPreviewSnapshot.objects.create(
        owner=owner, since=since, until=until, view=view, payload=_json(payload),
        session_ids=[r["id"] for r in described], truncated=truncated, expires_at=timezone.now() + TTL)


def live_for(owner, snapshot_id) -> BrainPreviewSnapshot | None:
    """`owner`'s snapshot `snapshot_id`, unexpired, else None: anyone else's is not found."""
    try:
        pk = int(snapshot_id)
    except (TypeError, ValueError):
        return None
    return BrainPreviewSnapshot.objects.filter(pk=pk, owner=owner, expires_at__gt=timezone.now()).first()


def put_outputs(snapshot: BrainPreviewSnapshot, items: list[dict], *, readable: set[str]) -> list[dict]:
    """Replace the snapshot's recommendations with `items`, each `{kind, title, body, sessions, learner?}`. Every cited
    session must be in the snapshot and in `readable` (what its owner reads now, not removed); words are scrubbed,
    object keys too, as `brain_emit` does."""
    if len(items) > MAX_OUTPUTS:
        raise RefusedError(f"at most {MAX_OUTPUTS} outputs")
    rows = {r["id"]: r for r in snapshot.payload.get("sessions", [])}
    out = []
    now = timezone.now()
    for n, item in enumerate(items, start=1):
        try:
            cited = [str(uuid.UUID(str(s))) for s in item.get("sessions") or []]
        except ValueError as e:
            raise RefusedError(f"output {n} cites something that is not a session id") from e
        if not cited or len(cited) > MAX_CITED:
            raise RefusedError(f"output {n} must cite between 1 and {MAX_CITED} sessions")
        missing = [s for s in cited if s not in rows or s not in readable]
        if missing:
            raise RefusedError(f"output {n} cites sessions not in this preview: {', '.join(missing)}")
        out.append({"id": n, "learner": (item.get("learner") or DEFAULT_LEARNER)[:64], "kind": item["kind"][:40],
                    "title": redact.redact_text(item.get("title") or "")[0][:300], "body": _scrub(item.get("body") or {}),
                    "sessions": cited, "workspace": rows[cited[0]]["workspace"]["slug"], "created_at": now})
    snapshot.outputs = _json(out)
    snapshot.save(update_fields=["outputs"])
    return snapshot.outputs


def drop(snapshot: BrainPreviewSnapshot, gone: set[str]) -> bool:
    """Take `gone` sessions out of `snapshot`, with every link and recommendation that names them."""
    if not gone & set(snapshot.session_ids or []):
        return False
    p = snapshot.payload
    p["sessions"] = [r for r in p.get("sessions", []) if r["id"] not in gone]
    p["links"] = [x for x in p.get("links", []) if x["session_id"] not in gone]
    p["left_out"] = p.get("left_out", 0) + len(gone & set(snapshot.session_ids))
    snapshot.session_ids = [s for s in snapshot.session_ids if s not in gone]
    snapshot.outputs = [o for o in snapshot.outputs or [] if not gone & set(o["sessions"])]
    snapshot.save(update_fields=["payload", "session_ids", "outputs"])
    return True


def sweep(source_ids=None) -> int:
    """The cascade's store: drop every removed session from every live snapshot (narrowed to `source_ids`' sessions
    when given). Returns how many snapshots changed."""
    snaps = list(BrainPreviewSnapshot.objects.filter(expires_at__gt=timezone.now()))
    wanted = None
    if source_ids is not None:
        wanted = {str(s) for s in BrainSource.objects.filter(pk__in=list(source_ids), session_id__isnull=False)
                  .values_list("session_id", flat=True)}
        snaps = [s for s in snaps if wanted & set(s.session_ids or [])]
    ids = {i for s in snaps for i in s.session_ids or []}
    gone = removed_sessions(ids if wanted is None else ids & wanted)
    return sum(drop(s, gone) for s in snaps)


def expire() -> int:
    """Delete the snapshots past their week."""
    return BrainPreviewSnapshot.objects.filter(expires_at__lte=timezone.now()).delete()[0]


def readable_projects(owner, keys) -> set[str]:
    """Which project keys of a snapshot its owner may still read."""
    ids = [int(k) for k in keys if str(k).isdecimal()]
    return {str(pk) for pk in AgentProject.objects.filter(project_read_q(owner), pk__in=ids).values_list("pk", flat=True)}
