"""Which project a source belongs to: `SourceLink`s, made in layers, strongest wins.

1. **Dispatched** (certain): a turn raised from a task of the project ran in this session
   (`Turn.raised_from_task`, the session from `Turn.canopy_session` or `chat_session`: what
   `contacts.hcp.consent.project_of` reads).
2. **Signals**: the project's `ProjectSignature` matched against the session's recorded activity
   (`canopy_sessions.activity`: repos, edited paths, PRs, MCP tool calls, branches). `classify` is
   the classifier of `project_history` in the canopy repo, ported: strong signals make `core` (a
   lone owned PR makes `adjacent`), weak ones make a `candidate`.
3. **Judged**: a model's ruling on candidates, made by a judge run (`schedule.py`, `emit._emit_link`).
4. **Declared**: an agent at close-out or a person. Overrides all; a PERSON's rejection sticks.

Everything here reads stored metadata, never a message. A sealed source (sensitive, a contact's,
deleted words) is not looked at, and neither is one not everyone in agreed to `project`
(`eligibility.everyone_agreed`): layers 1 and 2 and the signature seed skip it; a declaration
stands. A link is metadata and confers no read by itself.

`run_due` is the one worker: the off-request tick (`apps/common/ticks.py`) calls it, and an
activity fold only marks the source due (`schedule`), debounced, so a burst of folds is one look."""
from __future__ import annotations

import datetime as dt
import fnmatch
import logging
from collections import Counter
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.agents.models import AgentProject
from apps.canopy_sessions.models import Session
from apps.harness.models import Turn

from . import participants, sources
from .eligibility import everyone_agreed
from .models import BRAIN_RUN_KEY, PROJECT, BrainSource, ProjectSignature, SourceLink

logger = logging.getLogger(__name__)

#: How long after an activity fold the source is looked at, so many folds are one look.
DEBOUNCE = dt.timedelta(minutes=5)
#: Sources one `run_due` pass takes, and sessions it makes a source for.
BATCH = 200


# ---- the classifier (ported from canopy's project_history) ---------------------------------------

def strip_repo(key: str, repo: str) -> str | None:
    """Activity path keys look like `connect-labs:connect_labs/x`; a key for ANOTHER repo is not a
    project path. A bare key passes through."""
    if ":" in key and not key.startswith("/"):
        r, rest = key.split(":", 1)
        if repo and r and r != repo:
            return None
        return rest
    return key


def path_matches(path: str, specs) -> bool:
    """True when `path` lies inside one of `specs` (repo-relative directories). Accepts an absolute
    path, a repo-relative one, or one relative to a parent of the spec directory (activity records
    some directories relative to where the edit ran: `supply_chain/stock` for
    `connect_labs/supply_chain`). Whole path segments only: `connect_labs/supply` never matches
    `connect_labs/supply_chain`."""
    p = path.strip().lstrip("./")
    if not p:
        return False
    padded = "/" + p.strip("/") + "/"
    for spec in specs:
        s = spec.strip("/")
        if not s:
            continue
        if "/" + s + "/" in padded:
            return True
        if path.startswith("/"):
            continue  # an absolute path must contain the whole spec
        segs = s.split("/")
        for k in range(1, len(segs)):
            suffix = "/".join(segs[k:])
            if p == suffix or p.startswith(suffix + "/"):
                return True
    return False


def classify(strong: list[str], weak: list[str], *, owned: int, edits: int, mcp: int, excluded_edits: int,
             mcp_min: int) -> tuple[str, list[str]]:
    """Tier from signals. Strong (did project work) is `core`; a lone owned PR with nothing else is
    `adjacent` (it touched the project once on its way to something else); weak-only is a
    `candidate`; edits only in excluded paths, or nothing at all, is no link ("")."""
    if strong:
        lone_pr = owned == 1 and edits == 0 and mcp < mcp_min
        return (SourceLink.ADJACENT if lone_pr else SourceLink.CORE), strong + weak
    if excluded_edits:
        return "", [f"edits only excluded paths ({excluded_edits})"] + weak
    if weak:
        return SourceLink.CANDIDATE, weak
    return "", []


def signals_of(activity: dict, sig: ProjectSignature) -> dict:
    """What a session's `activity` says about one project: PRs it created that are the project's
    (split into own and cross-cutting), project edits, excluded edits, project MCP calls."""
    own, cross = [], []
    for pr in activity.get("prs") or []:
        key = f"{pr.get('repo') or ''}#{pr.get('number')}"
        info = (sig.prs or {}).get(key)
        if info is not None and "created" in (pr.get("actions") or []):
            (cross if info.get("cross_cutting") else own).append(key)
    edits = excluded = 0
    for key, n in (activity.get("paths") or {}).items():
        rel = strip_repo(key, sig.repo)
        if rel is None:
            continue
        if path_matches(rel, sig.paths or []) and not path_matches(rel, sig.exclude_paths or []):
            edits += int(n or 0)
        elif sig.exclude_paths and path_matches(rel, sig.exclude_paths):
            excluded += int(n or 0)
    mcp = sum(int(n or 0) for tool, n in (activity.get("mcp_tools") or {}).items()
              if any(tool.split("__", 2)[-1].startswith(px) for px in (sig.mcp_prefixes or [])))
    return {"own": sorted(set(own)), "cross": sorted(set(cross)), "edits": edits, "excluded": excluded, "mcp": mcp}


def weak_hits(session, sig: ProjectSignature) -> list[str]:
    """Name terms in the session's title, branches and working folders, and branch patterns: the
    weak signals. Never a message."""
    act = session.activity or {}
    branches = list(act.get("branches") or [])
    hay = " ".join([session.title or "", *branches, *(act.get("cwds") or [])]).lower()
    hits = [t for t in (sig.name_terms or []) if t and t.lower() in hay]
    hits += [f"branch matches {pat}" for pat in (sig.branch_patterns or [])
             if any(fnmatch.fnmatch(b, pat) for b in branches)]
    return hits


def evaluate(session, sig: ProjectSignature) -> tuple[str, list[str]]:
    """`(tier, evidence)` for one session against one project's signature; tier "" means no link."""
    s = signals_of(session.activity or {}, sig)
    strong = []
    if s["own"]:
        strong.append(f"owns {len(s['own'])} project PR(s)")
    if s["edits"]:
        strong.append(f"edits project paths ({s['edits']})")
    if s["mcp"] >= sig.mcp_min:
        strong.append(f"calls project MCP tools ({s['mcp']})")
    weak = weak_hits(session, sig) + ([f"owns {len(s['cross'])} cross-cutting PR(s)"] if s["cross"] else [])
    return classify(strong, weak, owned=len(s["own"]), edits=s["edits"], mcp=s["mcp"],
                    excluded_edits=s["excluded"], mcp_min=sig.mcp_min)


# ---- writing links: one row per (source, project), the strongest layer wins -----------------------

def rank(link: SourceLink) -> int:
    """Declared by a person beats declared by an agent beats dispatched beats judged beats signals; a
    person's own rejection beats everything, which is what makes it stick."""
    if link.how == SourceLink.DECLARED:
        if link.by_id is not None:
            return 5 if link.tier == SourceLink.REJECTED else 4
        return 3
    return {SourceLink.DISPATCHED: 2, SourceLink.JUDGED: 1, SourceLink.SIGNALS: 0}[link.how]


def outranks(row: SourceLink | None, new: SourceLink) -> bool:
    """Does the stored link `row` hold its place against `new`: it outranks it, or is a declaration of equal
    rank by someone else (a person's rejection stays until that same person undoes it)."""
    if row is None:
        return False
    same_hand = (new.how == SourceLink.DECLARED and row.how == SourceLink.DECLARED
                 and row.by_id == getattr(new, "by_id", None))
    return not same_hand and (rank(row) > rank(new) or (rank(row) == rank(new) and new.how == SourceLink.DECLARED))


def put(source: BrainSource, project: AgentProject, *, how: str, tier: str, evidence=(), confidence=None,
        model: str = "", by=None, by_agent=None) -> SourceLink:
    """Make or update the link, unless the one there outranks it, or is a person's rejection and this
    is not that same person undoing it (`outranks`). Returns the row as it stands."""
    new = SourceLink(source=source, subject_type=SourceLink.PROJECT_SUBJECT, subject=str(project.pk), how=how,
                     tier=tier, evidence=list(evidence), confidence=confidence, model=model, by=by,
                     by_agent=by_agent)
    with transaction.atomic():
        row = (SourceLink.objects.select_for_update()
               .filter(source=source, subject_type=SourceLink.PROJECT_SUBJECT, subject=str(project.pk)).first())
        if row is None:
            new.save()
            return new
        if outranks(row, new):
            return row
        for field in ("how", "tier", "evidence", "confidence", "model", "by", "by_agent"):
            setattr(row, field, getattr(new, field))
        row.save()
        return row


def rejected_by_a_person(source: BrainSource, project: AgentProject) -> SourceLink | None:
    return SourceLink.objects.filter(source=source, subject_type=SourceLink.PROJECT_SUBJECT,
                                     subject=str(project.pk), how=SourceLink.DECLARED,
                                     tier=SourceLink.REJECTED, by__isnull=False).first()


def declare(source: BrainSource, project: AgentProject, *, tier: str, by=None, by_agent=None) -> SourceLink:
    """Layer 4. Exactly one of `by` (a person) and `by_agent`. Returns the row as it stands, which is
    the earlier one when it outranks this (a person's rejection, an earlier person's own call)."""
    assert (by is None) != (by_agent is None)
    return put(source, project, how=SourceLink.DECLARED, tier=tier, by=by, by_agent=by_agent,
               evidence=["declared by a person" if by is not None else f"declared by {by_agent.slug}"])


def undeclare(source: BrainSource, project: AgentProject, *, by) -> bool:
    """A person withdraws their own declaration; the automatic layers look again straight away."""
    n, _ = SourceLink.objects.filter(source=source, subject_type=SourceLink.PROJECT_SUBJECT,
                                     subject=str(project.pk), how=SourceLink.DECLARED, by=by).delete()
    if n:
        associate(source)  # at once: the row held the place of whatever the layers had found
    return bool(n)


# ---- the layers ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    """A link an automatic layer would make: computed, not stored. `tier` "" is a signals link that no
    longer holds (the writer deletes a signals link there, never a stronger one)."""

    project: AgentProject
    how: str
    tier: str
    evidence: tuple = ()


def _candidate_projects(workspace_id):
    """Workspace projects of the source's own workspace: a link never crosses a workspace, so a
    project's readers can never be handed a session of another tenant."""
    return AgentProject.objects.filter(workspace_id=workspace_id, visibility=AgentProject.WORKSPACE)


def propose_dispatched(session, workspace_id) -> list[Proposal]:
    """Layer 1, computed: one `core` link per project a turn raised from one of its tasks ran in `session`."""
    turns = (Turn.objects.filter(Q(canopy_session=session) | Q(chat_session=session), raised_from_task__isnull=False)
             .select_related("raised_from_task__project"))
    out, made = [], set()
    for turn in turns:
        task = turn.raised_from_task
        project = task.project if task is not None else None
        if project is None or project.is_personal or project.workspace_id != workspace_id or project.pk in made:
            continue
        made.add(project.pk)
        out.append(Proposal(project, SourceLink.DISPATCHED, SourceLink.CORE, (f"task:{task.pk}",)))
    return out


def propose_signals(session, workspace_id) -> list[Proposal]:
    """Layer 2, computed: every project of the workspace that has a signature, judged on `session`'s activity."""
    out = []
    for sig in ProjectSignature.objects.filter(project__in=_candidate_projects(workspace_id)).select_related("project"):
        tier, evidence = evaluate(session, sig)
        out.append(Proposal(sig.project, SourceLink.SIGNALS, tier, tuple(evidence)))
    return out


def propose(session, workspace_id) -> list[Proposal]:
    """What layers 1 and 2 would make for `session` (a `tier` of "" proposes nothing), stored nowhere: what
    `associate` writes and a preview shows. It asks no consent: the writer asks that first."""
    return propose_dispatched(session, workspace_id) + propose_signals(session, workspace_id)


def link_dispatched(source: BrainSource, session) -> int:
    """Layer 1: write what `propose_dispatched` computes."""
    made = propose_dispatched(session, source.workspace_id)
    for p in made:
        put(source, p.project, how=p.how, tier=p.tier, evidence=p.evidence)
    return len(made)


def link_signals(source: BrainSource, session) -> int:
    """Layer 2: write what `propose_signals` computes."""
    n = 0
    for p in propose_signals(session, source.workspace_id):
        if p.tier:
            row = put(source, p.project, how=p.how, tier=p.tier, evidence=p.evidence)
            n += row.how == SourceLink.SIGNALS
        else:  # what it looked like it was no longer holds: a signals link only, never a stronger one
            SourceLink.objects.filter(source=source, subject_type=SourceLink.PROJECT_SUBJECT,
                                      subject=str(p.project.pk), how=SourceLink.SIGNALS).delete()
    return n


def associate(source: BrainSource) -> bool:
    """Look at one source: refresh who is in it, then layer 1, then layer 2. False when it is sealed, has
    no session, or not everyone in it agreed to `project`: the automatic layers never judge a session
    unless everyone in it agreed (a person's own declaration still works)."""
    session = source.session
    now = timezone.now()
    if session is not None:
        sources.refresh(source, session)
    if session is None or participants.sealed_reason(session) or not everyone_agreed(source, PROJECT):
        BrainSource.objects.filter(pk=source.pk).update(associate_due_at=None, associated_at=now)
        return False
    link_dispatched(source, session)
    link_signals(source, session)
    BrainSource.objects.filter(pk=source.pk).update(associate_due_at=None, associated_at=now)
    return True


def schedule(session_id, *, delay: dt.timedelta = DEBOUNCE) -> int:
    """An activity fold changed this session: look at it after `delay`. A source already due keeps its
    earlier time, which is the debounce."""
    return BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(session_id),
                                      associate_due_at__isnull=True).update(
        associate_due_at=timezone.now() + delay)


def run_due(limit: int = BATCH) -> dict:
    """One pass: make a source for sessions that have none, then look at the sources never looked at
    or now due. Safe to run from any number of workers; a second look is a no-op."""
    made = 0
    # A learning run's own conversation is never a source (`participants.sealed_reason`): none is made for it.
    fresh = Session.objects.filter(brain_sources__isnull=True).exclude(metadata__has_key=BRAIN_RUN_KEY)
    for session in fresh.order_by("-updated_at")[:limit]:
        try:
            sources.for_session(session)
            made += 1
        except Exception:  # noqa: BLE001 — one bad session (a race on its source) must not stop the pass
            logger.exception("could not make a source for session %s", session.pk)
    now = timezone.now()
    due = list(BrainSource.objects.filter(kind=BrainSource.SESSION, state=BrainSource.OPEN)
               .filter(Q(associated_at__isnull=True) | Q(associate_due_at__lte=now))
               .select_related("session").order_by("associate_due_at", "pk")[:limit])
    looked = 0
    for source in due:
        try:
            looked += associate(source)
        except Exception:  # noqa: BLE001 — one bad session must not stop the pass; it is looked at again
            logger.exception("could not associate source %s", source.pk)
    return {"made": made, "looked": looked}


# ---- seeding a signature from the sessions certainly on the project ------------------------------

#: Servers whose tools every session calls: no signal about any one project.
_GENERIC_SERVERS = ("canopy",)
_STOP_TERMS = frozenset({"project", "the", "and", "for", "new", "test", "app", "web", "data"})


def _mcp_prefix(tool: str) -> str:
    parts = tool.split("__", 2)
    if len(parts) < 3 or any(g in parts[1] for g in _GENERIC_SERVERS):
        return ""
    words = parts[2].split("_")
    return "_".join(words[:2]) + "_" if len(words) > 2 else ""


def seed_signature(project: AgentProject, *, min_sessions: int = 2) -> ProjectSignature:
    """Make or widen the project's signature from its `core` links that are certain (dispatched or
    declared by someone, never from signals: a signal-made link must not feed back into the signal).
    Never removes anything, so a hand-added entry stays."""
    certain = (SourceLink.objects.filter(subject_type=SourceLink.PROJECT_SUBJECT, subject=str(project.pk),
                                         tier=SourceLink.CORE, how__in=[SourceLink.DISPATCHED, SourceLink.DECLARED])
               .values_list("source__session_id", flat=True))
    # Only sessions everyone in agreed to `project`: a signature is an automatic layer too.
    sessions = [s for s in Session.objects.filter(pk__in=list(certain))
                if (src := sources.existing(s)) is not None and everyone_agreed(src, PROJECT)]
    sig, _ = ProjectSignature.objects.get_or_create(project=project)
    need = max(min_sessions, -(-len(sessions) * 2 // 5))  # a directory or tool in two of five at least
    repos, remotes, dirs, prefixes = Counter(), Counter(), Counter(), Counter()
    prs: dict[str, dict] = dict(sig.prs or {})
    for s in sessions:
        act = s.activity or {}
        repos.update(set(act.get("repos") or []))
        remotes.update(set(act.get("remotes") or []))
        seen_dirs = {rel for key in (act.get("paths") or {}) if (rel := strip_repo(key, "")) and "/" in rel}
        dirs.update(seen_dirs)
        prefixes.update({p for t in (act.get("mcp_tools") or {}) if (p := _mcp_prefix(t))})
        for pr in act.get("prs") or []:
            if "created" in (pr.get("actions") or []):
                prs.setdefault(f"{pr.get('repo') or ''}#{pr.get('number')}", {"cross_cutting": False})
    if sessions:
        if not sig.repo and repos:
            sig.repo = repos.most_common(1)[0][0]
        if not sig.remote and remotes:
            sig.remote = remotes.most_common(1)[0][0]
    terms = {project.handle.lower(), (project.name or "").lower()} - {""}
    sig.paths = sorted({*(sig.paths or []), *(d for d, n in dirs.items() if n >= need)})
    sig.mcp_prefixes = sorted({*(sig.mcp_prefixes or []), *(p for p, n in prefixes.items() if n >= need)})
    sig.name_terms = sorted({*(sig.name_terms or []), *(t for t in terms if t not in _STOP_TERMS)})
    sig.prs = prs
    sig.seeded_at = timezone.now()
    sig.save()
    return sig


def compare(project: AgentProject, truth, *, tiers=(SourceLink.CORE,), how: str = "") -> dict:
    """Precision and recall of the project's stored links (those made `how`, when named) against a hand-checked
    set of session ids: how a signature, or a judge, is validated before it is trusted."""
    truth = {str(t) for t in truth}
    links = SourceLink.objects.filter(subject_type=SourceLink.PROJECT_SUBJECT, subject=str(project.pk),
                                      tier__in=list(tiers))
    if how:
        links = links.filter(how=how)
    chosen = set(links.values_list("source__ref", flat=True))
    hits = chosen & truth
    return {"selected": len(chosen), "truth": len(truth), "hits": len(hits),
            "precision": round(len(hits) / len(chosen), 3) if chosen else None,
            "recall": round(len(hits) / len(truth), 3) if truth else None,
            "extra": sorted(chosen - truth), "missed": sorted(truth - chosen)}
