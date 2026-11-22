"""The brain as it stands, for one person (`GET /api/brain/state`, `/state/links`, `/state/runs`): the `/brain` page.

Reads only, every figure a SQL aggregate or one read per section, never a word of a session. What a person sees is
bounded by what they already read:

* **Sessions** only through legs 1 to 6 (`access.readable_sessions(brain=False)`): a link or a run's read of a
  session they cannot read is a count, never its id or title.
* **Projects** only ones they may read (`project_read_q`), in their own workspaces.
* **Workspace-wide counts** (sources, removals, cascade work) only where they hold `logs.read` (the health
  dashboard's gate); elsewhere the same counts over their own sources and their own removals.
* **Consent** as counts per workspace and class; nobody's name, and a count below `SMALL` is hidden (None, "<3")
  so a small workspace's few are not named by elimination. The viewer's own classes are theirs.
* **Learners** are what `/api/brain/capabilities/` already lists, with who answers for each (`registry.accountable`);
  who operates and answers for one only inside its operator's organisation (`registry.shown_to`).
* **Runs** of a feature about a project they read or about themselves; every run to a canopy admin.

`tests/test_brain_page.py` holds the routes to no write at all."""
from __future__ import annotations

import datetime as dt

from django.db.models import CharField, Count, Q
from django.db.models.functions import Cast
from django.utils import timezone

from apps.agents.models import AgentProject
from apps.agents.services.projects import project_read_q
from apps.canopy_sessions import access
from apps.common import redact
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import readers, registry
from .api_manage import flags_out, learner_refusal, may_manage, reads_out, targets_out
from .eligibility import covers
from .models import (
    CLASSES,
    PROJECT,
    SYSTEM,
    BrainCapability,
    BrainOutput,
    BrainRun,
    BrainRunRead,
    BrainSource,
    LearningConsent,
    RederiveRequest,
    RevocationNotice,
    SourceExclusion,
    SourceLink,
)

#: How far back "recent" removals reach.
RECENT = dt.timedelta(days=7)
#: Rows one page of links or runs answers at most.
MAX_LIMIT = 100
#: A consent count below this is hidden (None): in a small workspace a count of 1 or 2 names people.
SMALL = 3


def _hide_small(n: int) -> int | None:
    return n if n >= SMALL else None


def _name(u) -> str:
    return (u.get_full_name() or u.email or u.username) if u is not None else ""


def _admin_slugs(user, slugs) -> set[str]:
    """Where `user` sees workspace-wide counts: the workspaces of `slugs` they hold `logs.read` in."""
    return perms.slugs_with(user, perms.LOGS_READ, within=set(slugs))


def _readable_projects(user, slugs):
    return AgentProject.objects.filter(project_read_q(user), workspace_id__in=slugs)


def _readable_sessions(user, slugs):
    """Legs 1 to 6: never the brain's own project leg."""
    return access.readable_sessions(user, workspace_slugs=slugs, brain=False)


# ---- the summary ------------------------------------------------------------------------------------

def consent_counts(user, slugs, admin: set[str]) -> dict:
    """Per workspace the viewer holds `logs.read` in: how many members agreed to each class for it
    (`eligibility.covers`); and the viewer's own classes. Counts only, never a name; nothing for a workspace the viewer
    is only a member of."""
    workspaces = list(Workspace.objects.filter(slug__in=admin).order_by("slug"))
    members = {ws.slug: wsvc.member_user_ids(ws) for ws in workspaces}
    everyone = set().union(*members.values()) if members else set()
    consents = list(LearningConsent.objects.filter(user_id__in=everyone, revoked_at__isnull=True))
    rows = []
    for ws in workspaces:
        mine = [c for c in consents if c.user_id in members[ws.slug] and covers(c, ws.slug)]
        rows.append({"workspace": ws.slug, "name": ws.display_name, "members": len(members[ws.slug]), "small": SMALL,
                     "agreed_any": _hide_small(len({c.user_id for c in mine if c.classes})),
                     "agreed": {cls: _hide_small(sum(1 for c in mine if cls in (c.classes or []))) for cls in CLASSES}})
    own = next((c for c in consents if c.user_id == user.pk), None) or LearningConsent.objects.filter(
        user=user, revoked_at__isnull=True).first()
    return {"mine": list(own.classes) if own else [], "workspaces": rows}


def _scoped(qs, admin: set[str], slugs, *, workspace: str, mine: Q):
    """`qs` over the admin workspaces whole, and over the rest only where `mine` holds."""
    rest = set(slugs) - admin
    return qs.filter(Q(**{f"{workspace}__in": admin}) | (Q(**{f"{workspace}__in": rest}) & mine))


def source_counts(user, slugs, admin: set[str]) -> list[dict]:
    """Per workspace: open and withdrawn sources and the sealed ones by why (what the last refresh saw)."""
    qs = _scoped(BrainSource.objects.all(), admin, slugs, workspace="workspace_id",
                 mine=Q(pk__in=BrainSource.objects.filter(participants__user=user).values("pk")))
    out: dict[str, dict] = {}
    for ws, state, sealed, n in (qs.values_list("workspace_id", "state", "sealed_reason")
                                 .annotate(n=Count("pk")).order_by()):
        row = out.setdefault(ws, {"workspace": ws, "scope": "workspace" if ws in admin else "yours",
                                  "open": 0, "withdrawn": 0, "sealed": {}})
        row["open" if state == BrainSource.OPEN else "withdrawn"] += n
        if sealed:
            row["sealed"][sealed] = row["sealed"].get(sealed, 0) + n
    return [out[k] for k in sorted(out)]


def _links(user, slugs):
    """Project links of sessions the viewer reads (legs 1 to 6), to projects they may read."""
    keys = _readable_projects(user, slugs).annotate(key=Cast("pk", CharField())).values("key")
    return SourceLink.objects.filter(subject_type=SourceLink.PROJECT_SUBJECT, subject__in=keys,
                                     source__session__in=_readable_sessions(user, slugs).values("pk"))


def _projects(user, slugs, keys) -> dict[str, AgentProject]:
    """The projects of `keys` the viewer may read, by key; any other is left out."""
    ids = [int(k) for k in keys if k.isdecimal()]
    return {str(p.pk): p for p in _readable_projects(user, slugs).filter(pk__in=ids)}


def link_groups(user, slugs, admin: set[str]) -> list[dict]:
    """Stored links by project and tier: every link on a readable project of a workspace the viewer holds `logs.read`
    in (counts: no session is named by them), and elsewhere the links of sessions the viewer reads."""
    keys = (_readable_projects(user, admin).annotate(key=Cast("pk", CharField())).values("key"))
    found = SourceLink.objects.filter(Q(pk__in=_links(user, slugs).values("pk"))
                                      | Q(subject_type=SourceLink.PROJECT_SUBJECT, subject__in=keys))
    counts = list(found.values_list("subject", "tier").annotate(n=Count("pk")).order_by())
    projects = _projects(user, slugs, {s for s, _t, _n in counts})
    rows = [{"project": p.handle, "name": p.name, "workspace": p.workspace_id, "tier": tier, "count": n}
            for subject, tier, n in counts if (p := projects.get(subject)) is not None]
    return sorted(rows, key=lambda r: (r["workspace"], r["project"], r["tier"]))


def learners(user=None, pin: str = "", targets: list[dict] = ()) -> list[dict]:
    """Every learner, whether it can be aimed and run at all (`runnable`), how many of `targets` (the page's) are its,
    and whether `user` may configure it at `pin` (`api_manage.learner_refusal`)."""
    caps = list(BrainCapability.objects.select_related("operator", "registered_by").order_by("slug"))
    shown = registry.shown_to(user, caps) if user is not None else set()
    return [{"slug": c.slug, "name": c.name, "purpose_class": c.purpose_class, "enabled": c.enabled, "scope": c.scope,
             "operator": c.operator.slug if c.operator_id and c.pk in shown else "",
             "entry": c.entry if c.pk in shown else "",
             "accountable": _name(registry.accountable(c)) if c.pk in shown else "",
             "runnable": c.purpose_class in (PROJECT, SYSTEM) and c.slug != readers.PROJECT_READERS,
             "targets": sum(1 for t in targets if t["learner"] == c.slug),
             "configurable": bool(pin and user is not None and not learner_refusal(user, pin, c))} for c in caps]


def _runs(user, slugs, admin: set[str]):
    """The runs the viewer sees at this scope: those started for a workspace they hold `logs.read` in (a run started
    for an ancestor is not theirs to see unless they hold it there too), project runs on its projects, and runs about
    the viewer themselves. A canopy admin sees every run of the scope and the unscoped ones."""
    where = set(admin) | (set(slugs) if getattr(user, "is_superuser", False) else set())
    keys = _readable_projects(user, where).annotate(key=Cast("pk", CharField())).values("key")
    q = (Q(scope__in=where) | Q(subject_type="project", subject__in=keys, scope="")
         | Q(subject_type="person", subject=str(user.pk)))
    if getattr(user, "is_superuser", False):
        q |= Q(scope="", subject_type="system")
    return BrainRun.objects.filter(q)


def run_counts(user, slugs, admin: set[str]) -> dict:
    got = dict(_runs(user, slugs, admin).values_list("state").annotate(n=Count("pk")).order_by())
    return {s: got.get(s, 0) for s in (BrainRun.OPEN, BrainRun.DONE, BrainRun.FAILED)}


def cascade_counts(user, slugs, admin: set[str]) -> list[dict]:
    """Per workspace: removal work still to do and what was removed lately. Counts, never what."""
    since = timezone.now() - RECENT
    yours = Q(pk__in=BrainOutput.objects.filter(subject_type="person", subject=str(user.pk)).values("pk"))
    out: dict[str, dict] = {}

    def add(field, pairs):
        for ws, n in pairs:
            out.setdefault(ws, {"workspace": ws, "scope": "workspace" if ws in admin else "yours",
                                "rederive_open": 0, "revocations_undelivered": 0, "removed_recently": 0,
                                "outputs_withdrawn_recently": 0, "outputs_stale_recently": 0})[field] = n

    def per_ws(qs, ws_field):
        return qs.values_list(ws_field).annotate(n=Count("pk")).order_by()

    add("rederive_open", per_ws(_scoped(RederiveRequest.objects.filter(done_at__isnull=True), admin, slugs,
                                         workspace="output__workspace_id", mine=Q(output__in=BrainOutput.objects
                                                                                   .filter(yours))),
                                "output__workspace_id"))
    add("revocations_undelivered", per_ws(RevocationNotice.objects.filter(delivered_at__isnull=True,
                                                                          source__workspace_id__in=admin),
                                          "source__workspace_id"))
    add("removed_recently", per_ws(_scoped(SourceExclusion.objects.filter(created_at__gte=since), admin, slugs,
                                           workspace="source__workspace_id", mine=Q(user=user)),
                                   "source__workspace_id"))
    for field, state in (("outputs_withdrawn_recently", BrainOutput.WITHDRAWN),
                         ("outputs_stale_recently", BrainOutput.STALE)):
        add(field, per_ws(_scoped(BrainOutput.objects.filter(state=state, updated_at__gte=since), admin, slugs,
                                  workspace="workspace_id", mine=yours), "workspace_id"))
    return [out[k] for k in sorted(out)]


def summary(user, slugs, pin: str = "") -> dict:
    slugs = set(slugs)
    admin = _admin_slugs(user, slugs)
    aimed = [t for t in targets_out(user, pin) if t["workspace"] in slugs] if pin else []
    return {"learning_reads": reads_out(user, pin) if pin else None, "workspace": pin,
            "can_manage": may_manage(user, pin), "workspaces": sorted(slugs), "admin_workspaces": sorted(admin),
            "canopy_admin": bool(getattr(user, "is_superuser", False)),
            "consent": consent_counts(user, slugs, admin), "sources": source_counts(user, slugs, admin),
            "links": link_groups(user, slugs, admin), "learners": learners(user, pin, aimed), "targets": aimed,
            "flags": flags_out(user, pin, slugs) if pin else [],
            "runs": run_counts(user, slugs, admin),
            "cascade": cascade_counts(user, slugs, admin)}


# ---- the paged lists ----------------------------------------------------------------------------------

def _limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def link_page(user, slugs, *, before: int = 0, limit: int = 50) -> dict:
    """Stored links, newest first, each with its session (title scrubbed) and its project."""
    limit = _limit(limit)
    slugs = set(slugs)
    qs = _links(user, slugs).select_related("source__session").order_by("-pk")
    if before:
        qs = qs.filter(pk__lt=before)
    page = list(qs[:limit + 1])
    rows, projects = page[:limit], _projects(user, slugs, {lnk.subject for lnk in page[:limit]})
    out = []
    for lnk in rows:
        s, p = lnk.source.session, projects.get(lnk.subject)
        if p is None or s is None:
            continue
        out.append({"id": lnk.pk, "project": p.handle, "name": p.name, "workspace": s.workspace_id, "tier": lnk.tier,
                    "how": lnk.how, "confidence": lnk.confidence, "evidence": [str(e) for e in lnk.evidence or []],
                    "session_id": s.pk, "title": redact.redact_text(s.title or "")[0], "started_at": s.created_at})
    return {"links": out, "next_before": rows[-1].pk if rows else 0, "done": len(page) <= limit}


def context_shown(run: BrainRun) -> dict | None:
    """The context document a run was started with (`context.document`), as the page shows it: the ids of the
    sessions due left out (their count stays), since the viewer may not read them, and the open and dismissed findings
    it was told of (their counts stay), since who sees a run is not who sees its findings. None for a run from before
    it was kept."""
    if not run.context:
        return None
    return {**run.context, "source_ids_due": [], "open_findings": [], "dismissed_findings": []}


def run_page(user, slugs, *, before: int = 0, limit: int = 25) -> dict:
    """Runs, newest first, with what each read: the sessions the viewer reads by id and title, the rest a count."""
    limit = _limit(limit)
    slugs = set(slugs)
    qs = _runs(user, slugs, _admin_slugs(user, slugs)).select_related("capability").order_by("-pk")
    if before:
        qs = qs.filter(pk__lt=before)
    page = list(qs[:limit + 1])
    rows = page[:limit]
    reads: dict[int, list] = {}
    for run_id, session_id in (BrainRunRead.objects.filter(run__in=[r.pk for r in rows])
                               .values_list("run_id", "source__session_id")):
        reads.setdefault(run_id, []).append(session_id)
    every = {s for ids in reads.values() for s in ids if s}
    seen = {s.pk: s for s in _readable_sessions(user, slugs).filter(pk__in=every).only("pk", "title", "workspace_id")}
    projects = _projects(user, slugs, {r.subject for r in rows if r.subject_type == "project"})
    out = []
    for r in rows:
        got = reads.get(r.pk, [])
        shown = [seen[s] for s in got if s in seen]
        p = projects.get(r.subject) if r.subject_type == "project" else None
        subject = (p.handle if p is not None else "a project") if r.subject_type == "project" else (
            "you" if r.subject_type == "person" and r.subject == str(user.pk) else r.subject_type)
        out.append({"id": r.pk, "learner": r.capability.slug, "purpose": r.purpose, "subject_type": r.subject_type,
                    "subject": subject, "scope": r.scope, "state": r.state, "created_at": r.created_at, "finished_at": r.finished_at,
                    "reads": len(got), "sessions": [{"session_id": s.pk, "workspace": s.workspace_id,
                                                     "title": redact.redact_text(s.title or "")[0]} for s in shown],
                    "hidden": len(got) - len(shown), "context": context_shown(r)})
    return {"runs": out, "next_before": rows[-1].pk if rows else 0, "done": len(page) <= limit}
