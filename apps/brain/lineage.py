"""Lineage over everything a learning run makes, recorded by the server from the run turn's own credential.

A run's turn is an ordinary full-profile turn of its operator (`runs.start`), so its entry skill may make anything the
agent's turns may: tasks, project updates, documents, mail drafts or sends, pull requests. None of it is the skill's to
record. Every request that turn makes to canopy-web carries a credential naming it (a caller token's `turn_id`, or the
per-turn agent login `harness.turn_asker.serving_turn` recognises); `capturing`, a decorator on every API route
(registered in `apps/api/api.py` beside the settings audit), sees the rows such a write creates and records each as a
`BrainRunArtifact`, with `OutputLineage` (`output_kind` `brain.artifact`) to every source the run had read by then
(`BrainRunRead`). An MCP tool is the same route, so it is captured the same way. What the turn says it made outside
canopy (a PR, an issue, a link in its replies or its result) is recorded when the run ends (`record_reported`).
`brain_emit` outputs keep their own lineage (`emit`); they stay the default record of what was learned.

When a removal reaches a source (`cascade.sweep` calls `sweep`), each artifact made after the run read it is undone
where canopy can (`register_undo`: a task no one has started or finished is deleted, a draft is deleted) and FLAGGED
where it cannot (a sent email, a published update, a PR, anything without an undo): a flag is listed on the artifact's
workspace's brain page and raised to its owner, the person the run answered to (`alerts_removal`), until they
acknowledge it. Ids and links only: no artifact row holds a word of what it says."""
from __future__ import annotations

import contextvars
import functools
import logging
import re

from django.apps import apps as django_apps
from django.db import transaction
from django.db.models.signals import post_save
from django.utils import timezone

from .models import BrainRun, BrainRunArtifact, BrainRunRead, BrainSource, OutputLineage

logger = logging.getLogger(__name__)

#: `OutputLineage.output_kind` of an artifact.
ARTIFACT_KIND = "brain.artifact"
#: Rows that are plumbing, not something the run made: logs, audit, tokens, sessions and what every write leaves.
SKIP_APPS = frozenset({"brain", "events", "mcp", "tokens", "sessions", "admin", "contenttypes", "auth", "attention",
                       "push", "realtime", "timeline", "passkeys"})
SKIP_MODELS = frozenset({"harness.turnevent", "harness.callertoken", "harness.turnagenttoken",
                         "canopy_sessions.sessionparticipant", "canopy_sessions.draft", "canopy_sessions.chatkey",
                         "canopy_sessions.sessionkey", "canopy_sessions.sessionkeywrap", "harness.turntranscript",
                         "agents.taskevent", "agents.tasksettings", "agents.engagementevent",
                         "agents.agentcredentialread", "agents.projectparticipant"})
#: How many links one run's end records at most.
MAX_REPORTED = 50
_URL = re.compile(r"https?://[^\s<>()\"'`\]]+")
_GITHUB = re.compile(r"^https://github\.com/[^/]+/[^/]+/(pull|issues)/\d+")

_created: contextvars.ContextVar = contextvars.ContextVar("brain_run_created", default=None)
_UNDO: dict = {}


def register_undo(label: str, fn) -> None:
    """`fn(obj) -> bool` undoes one row of model `label` (`app_label.model`) that a removed source fed: True when it
    did (the row is withdrawn), False when it cannot (it is flagged). Registered by the app that owns the model."""
    _UNDO[label.lower()] = fn


# ---- which run a request is ------------------------------------------------------------------------------------

def turn_of_request(request):
    """The turn this request is made inside, from its own credential, or None."""
    from apps.harness import turn_asker  # noqa: PLC0415 — harness reads this app's neighbours
    from apps.harness.models import Turn  # noqa: PLC0415 — as above

    cred = getattr(request, "auth_credential", None) or {}
    if cred.get("turn_id"):
        return Turn.objects.select_related("chat_session").filter(pk=cred["turn_id"]).first()
    user = getattr(request, "user", None)
    return turn_asker.serving_turn(user) if getattr(user, "is_authenticated", False) else None


def run_of_turn(turn) -> BrainRun | None:
    """The open run whose own conversation `turn` is in, or None."""
    if turn is None or not turn.chat_session_id:
        return None
    return (BrainRun.objects.select_related("capability", "capability__operator")
            .filter(session_id=turn.chat_session_id, state=BrainRun.OPEN).first())


def _on_save(sender, instance, created, **kwargs):
    bucket = _created.get()
    if created and bucket is not None:
        bucket.append(instance)


post_save.connect(_on_save, dispatch_uid="brain.lineage.capture")


def _refused(result) -> bool:
    status = getattr(result, "status_code", None)
    if isinstance(result, tuple) and result and isinstance(result[0], int):
        status = result[0]
    return isinstance(status, int) and status >= 400


def capturing(func):
    """Record what a learning run's turn creates through this route (a write, by that turn's credential)."""
    @functools.wraps(func)
    def wrapper(request, *args, **kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return func(request, *args, **kwargs)
        try:
            run = run_of_turn(turn_of_request(request))
        except Exception:  # noqa: BLE001 — finding the run never fails the request
            logger.exception("could not tell whether a request is a learning run's")
            run = None
        if run is None:
            return func(request, *args, **kwargs)
        bucket: list = []
        token = _created.set(bucket)
        try:
            result = func(request, *args, **kwargs)
        finally:
            _created.reset(token)
        if not _refused(result) and bucket:
            # In the request's own transaction: a write that rolls back leaves no artifact behind.
            _record_rows(run.pk, [(o._meta.label_lower, str(o.pk), getattr(o, "workspace_id", None)) for o in bucket])
        return result
    return wrapper


# ---- recording -------------------------------------------------------------------------------------------------

def _people(source: BrainSource) -> list[int]:
    from . import participants  # noqa: PLC0415 — participants reads canopy_sessions

    stored = {p.user_id for p in source.participants.all() if p.user_id is not None}
    live = {u for _h, u, _c in participants.people_of(source.session) if u is not None} if source.session else set()
    return sorted(stored | live)


def record(run: BrainRun, kind: str, ref: str, *, workspace: str | None = None) -> BrainRunArtifact | None:
    """One artifact of `run`, with lineage to every source the run has read so far. Idempotent."""
    ws = workspace or run.scope or (run.capability.operator.workspace_id if run.capability.operator_id else None)
    if not ws:
        return None
    owner = _owner(run)
    art, _ = BrainRunArtifact.objects.get_or_create(run=run, kind=kind[:80], ref=str(ref)[:500],
                                                    defaults={"workspace_id": ws, "owner": owner})
    for read in BrainRunRead.objects.filter(run=run).select_related("source__session"):
        OutputLineage.objects.get_or_create(output_kind=ARTIFACT_KIND, output_id=str(art.pk), source=read.source,
                                            defaults={"participant_scope": _people(read.source)})
    return art


def _owner(run: BrainRun):
    from apps.harness.models import Turn  # noqa: PLC0415 — harness reads this app's neighbours

    first = Turn.objects.filter(chat_session_id=run.session_id).order_by("created_at").first() if run.session_id \
        else None
    return getattr(first, "initiator_user", None)


def _record_rows(run_id: int, rows) -> None:
    run = BrainRun.objects.select_related("capability", "capability__operator").filter(pk=run_id).first()
    if run is None:
        return
    for label, pk, ws in rows:
        if label.split(".", 1)[0] in SKIP_APPS or label in SKIP_MODELS:
            continue
        try:
            with transaction.atomic():
                record(run, label, pk, workspace=ws if isinstance(ws, str) else None)
        except Exception:  # noqa: BLE001 — a record is never worth a failed request; the sweep fails safe
            logger.exception("could not record what learning run %s made (%s %s)", run_id, label, pk)


def record_reported(run: BrainRun) -> int:
    """At the run's end: the links its turn reported making outside canopy (in its replies and its result), each an
    artifact (`github.pull`, `github.issue` or `link`). canopy-web's own pages are its rows, already recorded."""
    from django.conf import settings  # noqa: PLC0415 — read once, here

    from apps.canopy_sessions.models import Message  # noqa: PLC0415 — canopy_sessions reads this app's readers
    from apps.harness.models import Turn  # noqa: PLC0415 — harness reads this app's neighbours

    if run.session_id is None:
        return 0
    texts = list(Message.objects.filter(session_id=run.session_id, role=Message.ASSISTANT)
                 .values_list("plaintext", flat=True)[:500])
    texts += list(Turn.objects.filter(chat_session_id=run.session_id).values_list("result_note", flat=True))
    own = tuple(h for h in (getattr(settings, "CANOPY_PUBLIC_BASE_URL", ""), "https://canopy.dimagi.com") if h)
    seen: list[str] = []
    for text in texts:
        for url in _URL.findall(text or ""):
            url = url.rstrip(".,;:")
            if url in seen or (own and url.startswith(own)):
                continue
            seen.append(url)
    n = 0
    for url in seen[:MAX_REPORTED]:
        m = _GITHUB.match(url)
        kind = ("github.pull" if m.group(1) == "pull" else "github.issue") if m else "link"
        if record(run, kind, m.group(0) if m else url) is not None:
            n += 1
    return n


# ---- the cascade's half ----------------------------------------------------------------------------------------

def undo(art: BrainRunArtifact, reason: str) -> str:
    """Withdraw `art` where canopy can, else flag it. Returns the new state; a state already reached stays."""
    if art.state != BrainRunArtifact.LIVE:
        return art.state
    state = BrainRunArtifact.FLAGGED
    fn = _UNDO.get(art.kind)
    if fn is not None:
        try:
            model = django_apps.get_model(art.kind)
            obj = model.objects.filter(pk=art.ref).first()
            state = BrainRunArtifact.WITHDRAWN if obj is None or fn(obj) else BrainRunArtifact.FLAGGED
        except Exception:  # noqa: BLE001 — an undo that fails is flagged, never silent
            logger.exception("could not undo %s %s", art.kind, art.ref)
    elif art.kind not in ("link", "github.pull", "github.issue"):
        model = _model(art.kind)
        if model is not None and not model.objects.filter(pk=art.ref).exists():
            state = BrainRunArtifact.WITHDRAWN                      # gone already: nothing left to undo
    BrainRunArtifact.objects.filter(pk=art.pk, state=BrainRunArtifact.LIVE).update(
        state=state, reason=reason[:200], changed_at=timezone.now())
    return state


def _model(label: str):
    try:
        return django_apps.get_model(label)
    except (LookupError, ValueError):
        return None


def sweep(source_ids=None, memo: dict | None = None) -> dict:
    """Undo or flag every live artifact one of whose sources is now removed for its run's learner (`cascade`)."""
    from collections import defaultdict  # noqa: PLC0415 — only the sweep needs it

    from .cascade import removed_reason  # noqa: PLC0415 — cascade calls this

    memo = {} if memo is None else memo
    live = BrainRunArtifact.objects.filter(state=BrainRunArtifact.LIVE).values_list("pk", flat=True)
    rows = OutputLineage.objects.filter(output_kind=ARTIFACT_KIND, output_id__in=[str(i) for i in live])
    if source_ids is not None:
        rows = rows.filter(source_id__in=list(source_ids))
    by_art = defaultdict(list)
    for row in rows.select_related("source__session"):
        by_art[row.output_id].append(row)
    arts = {str(a.pk): a for a in BrainRunArtifact.objects.filter(pk__in=[int(i) for i in by_art])
            .select_related("run__capability")}
    done = {"withdrawn": 0, "flagged": 0}
    for aid, lineage in by_art.items():
        art = arts.get(aid)
        if art is None:
            continue
        cap, purpose = art.run.capability, art.run.purpose
        subject = int(art.run.subject) if cap.subject_type == "person" and art.run.subject.isdecimal() else None
        why = next((r for r in (removed_reason(x.source, cap.purpose_class, subject, x.participant_scope or [],
                                               capability=cap.slug, purpose=purpose, memo=memo) for x in lineage) if r),
                   "")
        if why:
            state = undo(art, f"a session it came from was removed ({why})")
            done["withdrawn" if state == BrainRunArtifact.WITHDRAWN else "flagged"] += 1
    return done


# ---- undo for what this app's neighbours hold -------------------------------------------------------------------

def _undo_task(task) -> bool:
    """A task no one has started or finished is deleted; one a person moved on is theirs now, so it is flagged."""
    if task.status in (task.IN_PROGRESS, task.DONE):
        return False
    task.delete()
    return True


def _undo_update(update) -> bool:
    """A draft project update is deleted; a published one was read, so it is flagged."""
    if update.status != update.DRAFT:
        return False
    update.delete()
    return True


register_undo("agents.agenttask", _undo_task)
register_undo("agents.projectupdate", _undo_update)
