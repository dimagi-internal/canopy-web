"""How the brain lets people read sessions, and records that it did.

**The project leg.** A session tied to a project by a `core` `SourceLink`, whose source passes
`eligible_sources` for the built-in `project-readers` feature (every human in it agreed to the
`project` class, none excluded it, nothing sealed), is readable, read-only, by every PERSON who may read
that project (`agents.services.projects.project_read_q`), and only a project of the session's own workspace. Never
an agent's login: an agent reads a project's sessions through a brain run (`brain_read`), not through the session ACL. `canopy_sessions.access` asks `project_q`
and `audience` asks `project_reader_ids`; both answer only for sessions of a workspace with "Learning reads" on
(`learning_reads`, through `eligible_sources`).
Stored rows only, never `may_pull`, redacted on the way out (`canopy_sessions.transcript_read.readable_window`).

**The record.** Every read the brain allows leaves one `brain.read` row in the event log of the
session's workspace (`record_read`): the feature, the purpose, the source ids, the turn. Reads of one
session by one reader coalesce into one row with a count; ids only, never a word."""
from __future__ import annotations

import logging

from django.db.models import CharField, Exists, OuterRef, Q
from django.db.models.functions import Cast

from apps.agents.models import Agent, AgentProject
from apps.agents.services.projects import project_read_q
from apps.events import services as events
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import learning_reads
from .eligibility import eligible_sources
from .models import BrainSource, SourceLink
from .participants import is_agent_login

logger = logging.getLogger(__name__)

#: The feature a project's readers read under (`migrations/0005_builtin_learner.py`).
PROJECT_READERS = "project-readers"
READ = "read"
READ_EVENT = "brain.read"
EVENT_SOURCE = "brain.read"


def _core_links():
    return SourceLink.objects.filter(subject_type=SourceLink.PROJECT_SUBJECT, tier=SourceLink.CORE)


def project_q(user) -> Q:
    """The sessions `user` reads through the project leg, as a `Q` on `Session`. Empty while no workspace has
    "Learning reads" on (a session's own workspace must: `eligible_sources`), for anyone signed out, for an agent's login, and for a user who reads no project."""
    if not learning_reads.any_on() or not getattr(user, "is_authenticated", False) or is_agent_login(user):
        return Q(pk__in=[])
    # A project of the session's OWN workspace only: a link left from before a move never opens a read elsewhere.
    readable = (AgentProject.objects.filter(project_read_q(user)).annotate(key=Cast("pk", CharField()))
                .filter(key=OuterRef("subject"), workspace_id=OuterRef("source__session__workspace_id")))
    eligible = eligible_sources(PROJECT_READERS, READ).values("session_id")
    return Q(pk__in=_core_links().filter(Exists(readable), source__session_id__in=eligible)
             .values("source__session_id"))


def project_reader_ids(session) -> set[int]:
    """Everyone the project leg lets read `session`: the readers of each project it is `core`-tied to,
    when its source is eligible. A workspace project's readers are the workspace's members; a personal
    one's is its lead. Never an agent's login (`project_q`). For `audience`, which holds this equal to
    the ACL."""
    if not learning_reads.any_on():
        return set()
    source = BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(session.pk)).first()
    if source is None or not eligible_sources(PROJECT_READERS, READ).filter(pk=source.pk).exists():
        return set()
    members = wsvc.member_user_ids(session.workspace_id)
    out: set[int] = set()
    for link in _core_links().filter(source=source):
        project = AgentProject.objects.filter(pk=link.subject).select_related("agent").first()
        if project is None or project.workspace_id != session.workspace_id:
            continue
        if not project.is_personal:
            out |= members
            continue
        if project.owner_user_id in members:
            out.add(project.owner_user_id)
    return out - set(Agent.objects.filter(user_id__in=out).values_list("user_id", flat=True))


def record_read(*, reader, capability: str, purpose: str, session, sources=(), turn_id: str = "") -> None:
    """One `brain.read` row in the session's workspace log. Never raises: a record is not worth a
    failed read, and the read has already been allowed by the time this runs."""
    try:
        ids = sorted({s.pk if hasattr(s, "pk") else s for s in sources}) or sorted(
            BrainSource.objects.filter(kind=BrainSource.SESSION, ref=str(session.pk)).values_list("pk", flat=True))
        events.record([{
            "source": EVENT_SOURCE, "kind": READ_EVENT,
            "key": f"{capability}:{getattr(reader, 'pk', '')}:{session.pk}",
            "payload": {"capability": capability, "purpose": purpose, "reader_id": getattr(reader, "pk", None),
                        "sources": ids, "turn": turn_id}}],
            workspace=Workspace(pk=session.workspace_id))
    except Exception:  # noqa: BLE001
        logger.exception("could not record a brain read")
