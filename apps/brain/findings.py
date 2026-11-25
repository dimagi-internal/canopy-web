"""An output's lifecycle with the people who act on it, and folding a repeat into the one already open.

`BrainOutput.state` is the cascade's (active, withdrawn, stale) and nobody sets it by hand. `status` is the people's:
`open` when made, then `accepted` (someone took it up), `dismissed` (not worth acting on) or `done` (acted on). Who may
set it (`may_set`) is someone who sees the output (`outputs.visible_to`, as the person) AND acts for its audience: a
`system` finding's workspace's owners and admins (or an ancestor's, `perms.BRAIN_MANAGE`) or a canopy admin; a
`project` output's project readers who are members of its workspace; an `individual` or `memory` output's own person.
An admin agent in its own turn (`actor.Actor`, `actor_may_set`) sets a `system` finding's where it and the person its
turn answers to manage learning, and a `project` output's where it reads the project and they manage learning there or
lead or administer the project; never an output about a person. Each change is an event in the output's workspace
naming who made it (and for an agent, its turn and its person).

A run names what an output IS with a `key` (`brain_emit`). An emit whose kind and key match an output of the same
learner, subject and workspace that is still active and open or accepted is a repeat (`match`): it folds in (`fold`),
counting `seen`, taking the newer words, adding a sighting (`sightings`) and adding this run's reads to the lineage, so a
removal of any session either run read still reaches it. A DISMISSED one takes a repeat the same way while it is within
its target's cooldown (`budget.dismiss_cooldown_days`, from when it was dismissed) and stays dismissed: the people said
it is not worth acting on, so seeing it again is a sighting, not news; after the cooldown a repeat is a new output. A
DONE one never takes a repeat: a repeat of it is a new output that names it (`recurs_of`, `came_back_of`), the finding
came back after it was acted on, which is how a learner's fixes are measured (`scorecard`). A run is shown the open
findings of its learner, subject and workspace (`open_for`, in its context) so it reuses their keys, and the ones
dismissed within the cooldown (`dismissed_for`) so it does not raise them again unless something changed; it may name
the one a finding repeats outright (`brain_emit`'s `same_as`, `match_id`).

How far a finding reaches (`breadth`): the distinct sessions and people across everything it cites, every fold
included (its cited `OutputLineage`, each row carrying the people in that session). A `system` finding seen in one
session is a `single session`, in two or more a `pattern`; a one-off is kept and labelled, never refused.

Two outputs that turn out to be one problem are merged (`merge`, by the same people who set a status): the second's
sightings and lineage move to the first, the second's status becomes `duplicate` with `merged_into` naming the first,
and a later repeat of either key folds into the first. An event in the workspace records it.

An open or accepted finding no run re-saw within the fade window of its type at its target (`budget.fade_days_for`:
friction fades in weeks, design in a quarter) is faded (`faded`): a computed state, nothing is deleted. It is not shown to runs as an existing finding, the page lists it below the fresh
ones, a repeat that folds into it makes it fresh again, and setting its status to open or accepted keeps it (`set_status`
moves its `last_seen_at` to now)."""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.agents.models import AgentProject
from apps.agents.services.projects import may_read_project, project_read_q
from apps.events import services as events
from apps.workspaces import services as wsvc

from . import actor as actor_mod
from . import budget, outputs
from .models import ABOUT_A_PERSON, PROJECT, SYSTEM, BrainOutput, BrainRun, BrainTarget, OutputLineage

logger = logging.getLogger(__name__)

OPEN, ACCEPTED, DISMISSED, DONE = "open", "accepted", "dismissed", "done"
STATUSES = (OPEN, ACCEPTED, DISMISSED, DONE)
#: Set only by `merge`, never through `set_status`.
DUPLICATE = "duplicate"
#: A repeat folds only into one still being worked on.
FOLDS_INTO = (OPEN, ACCEPTED)
#: How many open findings a run's context names at most (most recently seen first); `open_findings_total` counts all.
MAX_OPEN_IN_CONTEXT = 40
#: How many recently dismissed findings a run's context names at most (most recently dismissed first).
MAX_DISMISSED_IN_CONTEXT = 20
#: How many sightings an output keeps (the newest).
SIGHTINGS_KEPT = 200
#: How a `system` finding's breadth reads (`breadth_label`).
SINGLE_SESSION, PATTERN = "single session", "pattern"


def _ours(run: BrainRun, workspace: str | None):
    qs = BrainOutput.objects.filter(capability_id=run.capability_id, subject_type=run.subject_type,
                                    subject=run.subject, state=BrainOutput.ACTIVE)
    return qs.filter(workspace_id=workspace) if workspace else qs


def _cooldown_since(run: BrainRun, now=None):
    """The oldest dismissal that still takes repeats in, for `run`'s target."""
    return (now or timezone.now()) - dt.timedelta(days=budget.dismiss_cooldown_days(
        run.target if run.target_id else None))


def dismissed_at(out: BrainOutput):
    return out.status_at or out.created_at


def _takes_repeat(out: BrainOutput, since) -> bool:
    """Whether a repeat folds into `out`: open or accepted, or dismissed since `since` (the cooldown)."""
    if out.state != BrainOutput.ACTIVE:
        return False
    return out.status in FOLDS_INTO or (out.status == DISMISSED and dismissed_at(out) is not None
                                        and dismissed_at(out) >= since)


def _resolve(out: BrainOutput | None, since=None) -> BrainOutput | None:
    """A duplicate stands for what it was merged into (followed a few steps); None if that takes no repeat."""
    for _ in range(5):
        if out is None or out.status != DUPLICATE:
            break
        out = out.merged_into
    if out is None or not _takes_repeat(out, since if since is not None else timezone.now()):
        return None
    return out


def match(run: BrainRun, *, kind: str, key: str, workspace: str) -> BrainOutput | None:
    """The output a repeat of (`kind`, `key`) folds into, or None: an open one with that key, else one dismissed
    within the cooldown, else the one a duplicate with that key was merged into."""
    if not key:
        return None
    since = _cooldown_since(run)
    rows = _ours(run, workspace).filter(kind=kind, key=key)
    found = rows.filter(status__in=FOLDS_INTO).order_by("-pk").first()
    if found is None:
        found = (rows.filter(status=DISMISSED).filter(Q(status_at__gte=since)
                                                      | Q(status_at__isnull=True, created_at__gte=since))
                 .order_by("-pk").first())
    if found is None:
        found = _resolve(rows.filter(status=DUPLICATE).select_related("merged_into").order_by("-pk").first(), since)
    return found


def match_id(run: BrainRun, output_id: int, *, kind: str, workspace: str) -> BrainOutput | None:
    """The output a run names as the one its finding repeats (`same_as`): one of its learner, subject and workspace
    and kind, still open or accepted or dismissed within the cooldown (a duplicate stands for what it was merged
    into). None if it is not."""
    out = _ours(run, workspace).filter(pk=output_id, kind=kind).select_related("merged_into").first()
    return _resolve(out, _cooldown_since(run))


def came_back_of(run: BrainRun, *, kind: str, key: str, workspace: str) -> BrainOutput | None:
    """The `done` finding a new output of (`kind`, `key`) is a repeat of, the latest done, or None."""
    if not key:
        return None
    return (_ours(run, workspace).filter(kind=kind, key=key, status=DONE)
            .order_by(F("status_at").desc(nulls_last=True), "-pk").first())


def sighted(sightings, run: BrainRun | None, at=None) -> list[dict]:
    """`sightings` with one more: when, and by which run; the newest `SIGHTINGS_KEPT`."""
    rows = [s for s in (sightings if isinstance(sightings, list) else []) if isinstance(s, dict)]
    rows.append({"at": (at or timezone.now()).isoformat(), "run": getattr(run, "pk", None)})
    return rows[-SIGHTINGS_KEPT:]


def sighting_times(out: BrainOutput) -> list[str]:
    """When `out` was seen, oldest first: its sightings, or (one made before they were kept) when it was made and when
    it was last seen."""
    got = sorted(str(s["at"]) for s in (out.sightings or []) if isinstance(s, dict) and s.get("at"))
    if got:
        return got
    first = out.created_at.isoformat() if out.created_at else ""
    last = out.last_seen_at.isoformat() if out.last_seen_at else ""
    return [t for t in dict.fromkeys([first, last]) if t]


def faded(out: BrainOutput, days: int, now=None) -> bool:
    """An open or accepted finding no run re-saw in `days`."""
    if out.state != BrainOutput.ACTIVE or out.status not in FOLDS_INTO:
        return False
    seen = out.last_seen_at or out.created_at
    return seen is not None and seen < (now or timezone.now()) - dt.timedelta(days=days)


def _placed(out: BrainOutput, memo: dict) -> tuple[BrainTarget | None, dict[str, int]]:
    key = ("target", out.capability_id, out.workspace_id, getattr(out.run, "target_id", None) if out.run_id else None)
    if key not in memo:
        target = None
        if key[3] is not None:
            target = BrainTarget.objects.filter(pk=key[3]).first()
        if target is None:
            target = BrainTarget.objects.filter(capability_id=out.capability_id, workspace_id=out.workspace_id,
                                                project__isnull=True).first()
        memo[key] = (target, budget.fade_windows(target))
    return memo[key]


def target_of(out: BrainOutput, memo: dict | None = None) -> BrainTarget | None:
    """The target `out` belongs to: its run's, else its learner's in its workspace (a workspace target), else None."""
    return _placed(out, {} if memo is None else memo)[0]


def fade_days_of(out: BrainOutput, memo: dict | None = None) -> int:
    """The fade window of `out`: the window of its type (`budget.fade_days_for`) at its target (`target_of`)."""
    target, windows = _placed(out, {} if memo is None else memo)
    return budget.fade_days_for(target, out.body, windows)


def _fresh(rows, target, now=None):
    """The rows (`values` of open findings) not faded at `target`, by the window of each one's type."""
    now, windows = now or timezone.now(), budget.fade_windows(target)
    return [r for r in rows if (r["last_seen_at"] or r["created_at"])
            >= now - dt.timedelta(days=budget.fade_days_for(target, r["body"], windows))]


def open_for(run: BrainRun, *, workspace: str | None, limit: int = MAX_OPEN_IN_CONTEXT) -> tuple[int, list[dict]]:
    """The open and accepted findings of this run's learner, subject and workspace that have not faded (by the window
    of each one's type), most recently seen first: how many, and the first `limit` by id, key, title and sightings.
    What the run is told, so it reuses a key for the same problem."""
    target = run.target if run.target_id else None
    since = timezone.now() - dt.timedelta(days=budget.longest_fade(target))
    qs = (_ours(run, workspace).filter(status__in=FOLDS_INTO)
          .filter(Q(last_seen_at__gte=since) | Q(last_seen_at__isnull=True, created_at__gte=since)))
    rows = _fresh(qs.order_by(F("last_seen_at").desc(nulls_last=True), "-pk").values(
        "pk", "kind", "key", "title", "seen", "last_seen_at", "created_at", "body"), target)
    return len(rows), [{"id": r["pk"], "kind": r["kind"], "key": r["key"], "title": r["title"], "seen": r["seen"],
                        "last_seen_at": (r["last_seen_at"] or r["created_at"]).isoformat()} for r in rows[:limit]]


def dismissed_for(run: BrainRun, *, workspace: str | None, limit: int | None = None) -> tuple[int, list[dict]]:
    """The findings of this run's learner, subject and workspace dismissed within the cooldown, with a key, most
    recently dismissed first: how many, and the first `limit` by id, key and title. What the run is told not to raise
    again unless something changed (a repeat folds into the dismissed one and stays dismissed)."""
    since, limit = _cooldown_since(run), MAX_DISMISSED_IN_CONTEXT if limit is None else limit
    qs = (_ours(run, workspace).filter(status=DISMISSED).exclude(key="")
          .filter(Q(status_at__gte=since) | Q(status_at__isnull=True, created_at__gte=since)))
    rows = qs.order_by(F("status_at").desc(nulls_last=True), "-pk").values(
        "pk", "kind", "key", "title", "status_at", "created_at")[:limit]
    return qs.count(), [{"id": r["pk"], "kind": r["kind"], "key": r["key"], "title": r["title"],
                         "dismissed_at": (r["status_at"] or r["created_at"]).isoformat()} for r in rows]


def breadth(rows) -> dict[int, dict]:
    """Each output's reach across everything it cites, every fold included: distinct sessions and distinct people
    (the people in each cited session when it was cited, `OutputLineage.participant_scope`). One read for a page."""
    out: dict[int, dict] = {o.pk: {"sources": set(), "people": set()} for o in rows}
    for oid, src, people in (OutputLineage.objects.filter(output_kind="brain.output", cited=True,
                                                          output_id__in=[str(o.pk) for o in rows])
                             .values_list("output_id", "source_id", "participant_scope")):
        got = out.get(int(oid))
        if got is not None:
            got["sources"].add(src)
            got["people"].update(p for p in (people or []) if isinstance(p, int))
    return {k: {"sessions": len(v["sources"]), "people": len(v["people"])} for k, v in out.items()}


def breadth_label(out: BrainOutput, sessions: int) -> str:
    """A `system` finding's breadth in a word: `single session` or `pattern` (two or more); "" for any other."""
    if out.capability.purpose_class != SYSTEM:
        return ""
    return PATTERN if sessions >= 2 else SINGLE_SESSION


def fold(out: BrainOutput, run: BrainRun, *, title: str, body: dict, cited) -> BrainOutput:
    """Take a repeat into `out`: one more sighting, the newer words, and lineage to what this run read. Its status is
    left as it is (a dismissed one stays dismissed)."""
    from .emit import write_lineage  # noqa: PLC0415 — cycle: emit -> findings

    with transaction.atomic():
        locked = BrainOutput.objects.select_for_update().get(pk=out.pk)
        if locked.state != BrainOutput.ACTIVE:
            return locked                                        # withdrawn meanwhile: nothing to fold into
        locked.seen += 1
        locked.last_seen_at = timezone.now()
        locked.sightings = sighted(locked.sightings, run, locked.last_seen_at)
        if "merged_from" in (locked.body or {}) and isinstance(body, dict) and "merged_from" not in body:
            body = {**body, "merged_from": locked.body["merged_from"]}
        locked.title, locked.body = title or locked.title, body
        locked.save(update_fields=["seen", "last_seen_at", "sightings", "title", "body", "updated_at"])
        write_lineage(run, locked, cited=cited)
    return locked


def may_set(user, out: BrainOutput) -> bool:
    """May `user` (as themselves) change `out`'s status: they see it and act for its audience."""
    if not getattr(user, "is_authenticated", False) or out.state != BrainOutput.ACTIVE:
        return False

    if not outputs.visible_to(user, as_person=True).filter(pk=out.pk).exists():
        return False
    return acts_for(user, out)


def actor_may_set(actor, out: BrainOutput) -> bool:
    """`may_set` for a management `actor.Actor`: the person as `may_set`; an admin agent only for a `system` or
    `project` output, by the management rule."""
    if not actor.is_agent:
        return may_set(actor.user, out)
    if out.state != BrainOutput.ACTIVE:
        return False
    cls = out.capability.purpose_class
    if cls == SYSTEM:
        return actor.may_manage(out.workspace_id)
    if cls == PROJECT:
        from .api_manage import may_manage_target  # noqa: PLC0415 — cycle: api_manage -> api_admin -> findings

        pk = out.subject_project_id
        project = AgentProject.objects.filter(project_read_q(actor.user), pk=pk).first() if pk is not None else None
        return (project is not None and acts_for(actor.user, out)
                and may_manage_target(actor, out.workspace_id, project))
    return False


def acts_for(user, out: BrainOutput, memo: dict | None = None) -> bool:
    """`may_set` for an output already known to be visible to `user` as the person (a list's rows): whether they act
    for its audience. `memo` keeps the per-workspace and per-project answers across a page."""
    memo = {} if memo is None else memo
    cls = out.capability.purpose_class
    if cls == SYSTEM:
        key = ("manage", out.workspace_id)
        if key not in memo:
            memo[key] = actor_mod.may_manage(user, out.workspace_id)
        return memo[key]
    if cls == PROJECT:
        key = ("project", out.workspace_id, out.subject)
        if key not in memo:
            pk = out.subject_project_id
            project = AgentProject.objects.filter(pk=pk).first() if pk is not None else None
            memo[key] = (project is not None and wsvc.is_member(user, out.workspace_id)
                         and may_read_project(user, project))
        return memo[key]
    if cls in ABOUT_A_PERSON:
        return out.subject == str(user.pk)
    return False


def set_status(out: BrainOutput, status: str, *, by, acting: dict | None = None) -> BrainOutput:
    """Set `out`'s status as `by`; `acting` (`Actor.record`) names an agent, its turn and its person in the event."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if out.status == DUPLICATE:
        raise ValueError(f"output {out.pk} was merged into {out.merged_into_id}; set that one's status")
    keep = status in FOLDS_INTO and faded(out, fade_days_of(out))
    if status == out.status and not keep:
        return out
    was, now = out.status, timezone.now()
    BrainOutput.objects.filter(pk=out.pk).update(status=status, status_at=now, status_by=by,
                                                 **({"last_seen_at": now} if keep else {}))
    out.refresh_from_db()
    _record(out, was, by, acting or {})
    return out


def _record(out: BrainOutput, was: str, by, acting: dict, *, kind: str = "output.status", summary: str = "") -> None:
    try:
        events.record([{"source": "brain.output", "kind": kind,
                        "summary": summary or f"{out.capability.slug} output {out.pk}: {was} -> {out.status}",
                        "payload": {"output": out.pk, "from": was, "to": out.status, "by_id": getattr(by, "pk", None),
                                    **acting}}],
                      workspace=out.workspace)
    except Exception:  # noqa: BLE001 — a log line never fails the act
        logger.exception("could not record the status change of output %s", out.pk)


class MergeRefusedError(ValueError):
    """These two outputs cannot be merged (a 422 at the route), with why."""


def merge(keep: BrainOutput, dup: BrainOutput, *, by, acting: dict | None = None) -> BrainOutput:
    """Fold `dup` into `keep` as the same problem: `keep` takes its sightings (`seen` adds up, `last_seen_at` the later),
    its lineage (so a removal of a session either came from still reaches `keep`) and a note of it in its body
    (`merged_from`); `dup` becomes `duplicate`, `merged_into` `keep`, and a repeat of its key folds into `keep`. The
    caller has checked `by` may set both (`may_set` / `actor_may_set`)."""
    if keep.pk == dup.pk:
        raise MergeRefusedError("an output is not merged into itself")
    same = ("capability_id", "workspace_id", "subject_type", "subject")
    if any(getattr(keep, f) != getattr(dup, f) for f in same):
        raise MergeRefusedError("only outputs of the same learner, subject and workspace merge")
    with transaction.atomic():
        a = BrainOutput.objects.select_for_update().get(pk=keep.pk)
        b = BrainOutput.objects.select_for_update().get(pk=dup.pk)
        for o in (a, b):
            if o.state != BrainOutput.ACTIVE or o.status == DUPLICATE:
                raise MergeRefusedError(f"output {o.pk} is not one to merge (withdrawn, stale or merged already)")
        for row in OutputLineage.objects.filter(output_kind="brain.output", output_id=str(b.pk)):
            got, made = OutputLineage.objects.get_or_create(
                output_kind="brain.output", output_id=str(a.pk), source_id=row.source_id,
                defaults={"participant_scope": row.participant_scope, "cited": row.cited})
            if not made and row.cited and not got.cited:
                OutputLineage.objects.filter(pk=got.pk).update(cited=True)
        body = dict(a.body or {})
        note = {"id": b.pk, "key": b.key, "title": b.title, "seen": b.seen}
        if isinstance(b.body, dict):
            for k in ("evidence", "examples"):
                if isinstance(b.body.get(k), list) and isinstance(body.get(k, []), list):
                    body[k] = [*body.get(k, []), *(x for x in b.body[k] if x not in body.get(k, []))]
        body["merged_from"] = [*body.get("merged_from", []), note] if isinstance(body.get("merged_from"), list) else [note]
        seen_at = max([t for t in (a.last_seen_at or a.created_at, b.last_seen_at or b.created_at) if t])
        both = [{"at": t} for t in (*sighting_times(a), *sighting_times(b))]
        a.seen, a.last_seen_at, a.body = a.seen + b.seen, seen_at, body
        a.sightings = sorted(both, key=lambda x: x["at"])[-SIGHTINGS_KEPT:]
        a.save(update_fields=["seen", "last_seen_at", "sightings", "body", "updated_at"])
        was = b.status
        BrainOutput.objects.filter(pk=b.pk).update(status=DUPLICATE, merged_into=a, status_at=timezone.now(),
                                                   status_by=by)
        b.refresh_from_db()
    _record(b, was, by, {**(acting or {}), "merged_into": a.pk}, kind="output.merged",
            summary=f"{b.capability.slug} output {b.pk} merged into {a.pk} as the same problem")
    return a
