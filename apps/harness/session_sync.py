"""A runner's chat sessions, kept in step with canopy.

Which session a turn runs in (`resolve_session`, the thread/email bindings),
the session a runner reports it ran (`record_session`, `stamp_turn_session`),
the open sessions a runner reports wholesale (`replace_reported_sessions`) with
their blocked-agent menus, the live stream it ships (`ingest_session_stream`),
and the sessions a person may list. Split out of `services.py`, which keeps a
turn's lifecycle (enqueue, heartbeat, finish, cancel).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass

from canopy_transcript import BLOCK_STRIDE
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.canopy_sessions.staleness import active
from apps.workspaces import services as wsvc

from .models import ProjectOptIn, Runner, Turn
from .routing import EXECUTING, runner_tenant_slugs
from .runner_requirements import is_sensitive_data_box

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# RunnerBinding reuse — durable thread↔session mapping (cross-account); see
# apps.canopy_sessions.models.RunnerBinding
# --------------------------------------------------------------------------------------

def _binding_for_thread(agent, project, workspace, thread_key):
    """The RunnerBinding for a (target, thread_key), or None. Enforces the
    agent-XOR-project rule the way _link_target used to: an agent thread matches on
    session.agent and ignores workspace (derived via the agent); a project thread
    matches on session.project AND session.workspace (its identity, so a guessed
    thread_key from another tenant lands on its own row, never the victim's)."""
    if bool(agent) == bool(project):
        raise ValueError("a session reuse lookup targets an agent XOR a project")
    qs = RunnerBinding.objects.select_related("session", "runner").filter(thread_key=thread_key)
    if agent:
        return qs.filter(session__agent=agent).first()
    if workspace is None:
        raise ValueError("a project session reuse lookup needs a workspace")
    return qs.filter(
        session__agent__isnull=True, session__project=project, session__workspace=workspace
    ).first()


class ThreadSessionNotFound(LookupError):
    """A thread_key named an existing Session that is not this thread's to bind.
    The API answers 404 — the same as a session that does not exist — so a
    runner cannot learn which session ids are real."""


def _session_is_this_threads(session, agent, project, workspace, runner) -> bool:
    """May `runner` bind the EXISTING `session` as the thread (agent | project)?

    Binding re-points the session's live hint, its streams and its next turns at
    this box, so a session id must not be a key to someone else's conversation:
    it has to be a session OF this target — the same agent, or the same repo in
    the same workspace — in a workspace the runner's owner belongs to. Fails
    closed on a runner with no owner (no pairing human, no tenant)."""
    if agent is not None:
        if session.agent_id != agent.pk:
            return False
    elif (session.agent_id is not None or session.project != (project or "")
          or workspace is None or session.workspace_id != workspace.pk):
        return False
    owner = getattr(runner, "owner", None)
    return owner is not None and wsvc.member_role(owner, session.workspace_id) is not None


def _thread_session(agent, project, workspace, thread_key, *, runner, turn=None):
    """Find-or-create the durable Session a thread maps to. A chat thread_key is
    str(session.id) — bind that exact existing Session, when it is this thread's
    (`_session_is_this_threads`; else ThreadSessionNotFound). Otherwise create a
    durable origin=runner Session for the phone/agent/project thread.

    A new session made for a dispatched turn is that turn's work, so it takes the
    turn's parent and the parent conversation's people (`_adopt_turn_parent`).

    The Session's workspace is the one the caller pinned, else the agent's own
    home (Agent.workspace is NOT NULL). A thread with neither has no tenant, and
    guessing one would file it in a workspace nobody chose — so it fails loudly."""
    try:
        existing = Session.objects.filter(pk=uuid.UUID(str(thread_key))).first()
    except (ValueError, TypeError):
        existing = None
    if existing is not None:
        if not _session_is_this_threads(existing, agent, project, workspace, runner):
            raise ThreadSessionNotFound(thread_key)
        return existing
    home = workspace or (agent.workspace if agent else None)
    if home is None:
        raise ValueError("a new thread session needs an agent or a workspace")
    if turn is None:
        turn = (Turn.objects.filter(claimed_by=runner, origin_ref__thread_key=thread_key)
                .order_by("-created_at").first())
    parent = {}
    if turn is not None and (turn.parent_session_id or turn.parent_turn_id):
        parent = {"parent_session_id": turn.parent_session_id, "parent_turn_id": turn.parent_turn_id,
                  "parent_claude_session": turn.parent_claude_session}
    session = Session.objects.create(
        agent=agent,
        project=project or "",
        workspace=home,
        origin=Session.ORIGIN_RUNNER,
        title=thread_key[:200],
        **parent,
    )
    if parent:
        _adopt_turn_parent(session, turn)
    return session


def _adopt_turn_parent(session, turn) -> None:
    """The people of the conversation a turn was dispatched from join the session
    the runner made for it — the same rule as a session created in-app with a
    parent (`canopy_sessions.services._inherit_participants`): only from a parent
    the dispatcher can read (or whose live turn it is running), only workspace members, never as owner. Without it
    the work someone asked for in a chat never shows in their /ide."""
    from apps.canopy_sessions.services import _inherit_participants

    dispatcher = turn.enqueued_by or turn.initiator_user
    if dispatcher is not None and session.parent_session_id:
        _inherit_participants(session, dispatcher)


#: How an email thread names its Session. Namespaced like `emdash:<task>` so the
#: three producers of a thread_key cannot collide.
EMAIL_THREAD_PREFIX = "email:"

#: Where the Gmail thread id is remembered on the Session, so the SECOND message
#: on a thread finds the FIRST message's session instead of making a new one.
#: `_thread_session` cannot serve this: it looks a thread_key up only as a
#: Session UUID, so any non-UUID key creates a fresh row every single time.
EMAIL_THREAD_KEY = "email_thread_key"


def email_thread_session(agent, thread_id: str, subject: str = ""):
    """The durable Session for one Gmail thread — found, or created once.

    An inbound email already becomes a Turn. A Turn is a fine unit of execution
    and a poor unit of CONVERSATION: it ends, and there is nothing left for a
    human to open, watch, or type into. A Session is the thing ace-web already
    renders through the shared chat kit, so binding the two is what turns "ACE
    replied to that email eventually" into "here is the run, live, and you can
    steer it".

    Keyed on the Gmail thread, which gives the behaviour a reader expects for
    free: a reply on the same thread continues the same session; a new thread
    opens a parallel one.

    `origin_key` is stamped because that is the field ace-web's session list
    filters on — a session without it exists and is invisible, which is the
    least useful possible outcome.
    """
    if agent is None:
        raise ValueError("an email thread session belongs to an agent")
    key = f"{EMAIL_THREAD_PREFIX}{thread_id}"
    # A KEY-PATH lookup, not `metadata__contains`: `contains` on a JSONField is
    # PostgreSQL-only and raises NotSupportedError on SQLite, so the production
    # path would have worked while every test errored — found by running them.
    existing = (
        Session.objects.filter(agent=agent, **{f"metadata__{EMAIL_THREAD_KEY}": key})
        .order_by("created_at")
        .first()
    )
    if existing is not None:
        return existing
    workspace = agent.workspace  # NOT NULL: an agent always has its one home
    return Session.objects.create(
        agent=agent,
        workspace=workspace,
        origin=Session.ORIGIN_RUNNER,
        title=(subject or key)[:200],
        metadata={
            EMAIL_THREAD_KEY: key,
            # ace-web lists sessions by this; see its CLAUDE.md § canopy-hosted chat.
            "origin_key": f"ace-web:{workspace.slug}",
            "source": "email",
        },
    )


def _busy_reason(binding, turn_id) -> str:
    """Why the session `binding` points at must not take a prompt right now, or "".

    A prompt is only ever sent into an IDLE session (#309): typing into one that is
    mid-turn lands in the live turn's conversation and corrupts it. Two observations
    say busy, and neither is inferred from write recency (which would also fire for a
    session whose last turn ended a minute ago, forking every quick follow-up):

    - another EXECUTING turn holds it: a chat turn on the same Session, or an
      agent/project turn whose recorded session is this one on this box. Agent turns
      already serialize per agent (`one_executing_turn_per_agent`); project turns do
      not, and a chat turn is serialized per SESSION, not per agent.
    - the engine says it is working (`agent_status`, emdash's own flag, or the
      runner's `agent_status_stale` dissent): a person typing in it, or work that
      outlived its turn. A blank status (the runner could not answer) is not busy."""
    from apps.canopy_sessions import transcript_read

    others = Turn.objects.filter(status__in=EXECUTING).exclude(pk=turn_id)
    holder_q = Q(chat_session_id=binding.session_id)
    if binding.session_key and binding.runner_id:
        holder_q |= Q(session_key=binding.session_key, claimed_by_id=binding.runner_id)
    holder = others.filter(holder_q).values_list("pk", flat=True).first()
    if holder is not None:
        return f"turn {holder} is executing in it"
    if binding.agent_status and transcript_read.is_session_running(binding):
        return f"the engine reports it {binding.agent_status}"
    return ""


def resolve_session(agent, thread_key: str, runner: Runner, *, project: str = "", workspace=None,
                    turn_id=None) -> dict:
    """Given (target, thread_key) and the CURRENTLY-active runner, decide how to execute.

    `agent` may be None when `project` is given — the phone addresses repos too.

    Returns a plan dict:
      - reuse (bool): the live session hint is owned by THIS runner/host — the runner
        should verify the emdash task still exists and drive it (send prompt into it).
      - session_key: the session to reuse (only meaningful when reuse=True);
        `emdash_task_id` carries the same value for runners predating the rename.
      - agent_task_ext_id / summary: durable context for rehydration when reuse=False
        (fresh session under this account) or for a brand-new thread.
      - link_id: the RunnerBinding's session id (None if no binding exists yet — brand-new
        thread).

    Never assumes the live session is reachable: reuse is only proposed when the hint's
    runner + macOS host match the caller (the two-account failover invariant).

    `turn_id` names the agent/project turn asking. With it, reuse is also refused
    when the session is mid-turn (`_busy_reason`, #309) and `busy` says why; the
    runner then creates a fresh session, as it does for one that is gone. A CHAT
    turn is exempt: its session IS the conversation, which claim already
    serializes and a rider (#1153) joins on purpose. Without `turn_id` (a runner
    predating it) the answer is unchanged.

    `fork` ({session_key, records} or None) is set for a forked chat that has no
    Claude session yet: the runner starts it from the first `records` records of
    `session_key`'s transcript (`canopy_sessions.services.fork_session`).
    `worktree_of` names, for every turn of a fork, the chat whose directory a cloud
    runner runs it in — the source's, so both share one worktree. "" otherwise.
    `fork_lineage` is the fork and the sessions it was cut from, from canopy's own
    `parent_session` column; the runner runs in `worktree_of` only if it is there."""
    from apps.canopy_sessions.placement import fork_plan

    fork, worktree, lineage = fork_plan(thread_key)
    binding = _binding_for_thread(agent, project, workspace, thread_key)
    if binding is None:
        return {"reuse": False, "session_key": "", "emdash_task_id": "", "agent_task_ext_id": "",
                "summary": "", "link_id": None, "new_thread": True, "busy": "", "fork": fork,
                "worktree_of": worktree, "fork_lineage": lineage}
    reuse = binding.reusable_by(runner)
    busy = ""
    if reuse and turn_id is not None:
        # Only a chat turn THIS runner holds is exempt; any other id is guarded.
        turn = Turn.objects.filter(pk=turn_id, claimed_by=runner).only("chat_session_id").first()
        if turn is None or turn.chat_session_id is None:
            busy = _busy_reason(binding, turn_id)
            reuse = not busy
    return {
        "reuse": reuse,
        "busy": busy,
        "session_key": binding.session_key,
        "emdash_task_id": binding.session_key,
        "agent_task_ext_id": binding.agent_task_ext_id,
        "summary": binding.summary,
        "link_id": str(binding.session_id),
        "new_thread": False,
        "fork": fork,
        "worktree_of": worktree,
        "fork_lineage": lineage,
    }


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """Coerce a possibly-naive datetime to aware (UTC). The runner sends ISO8601
    (typically already UTC via a trailing "Z"), but a naive value would otherwise
    hit Django's USE_TZ=True as a silent local-time footgun rather than a clean
    UTC stamp."""
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, dt.UTC)
    return value


_COMMAND = re.compile(r"^/[\w:.-]+")          # "/ace:turn", "/eva:turn"
_FLAG = re.compile(r"^--?[\w-]+(?:[ =](?!--)\S+)?")  # "--thread 1a0f…", "--x=y"
TITLE_CAP = 80


def readable_title(text: str) -> str:
    """A name a person can read, from a prompt's first line — or "" when the line
    is nothing but a command. A cloud turn's prompt is often a slash command
    ("/ace:turn --thread 1a0f24bf9b830273"), which made a session's title the
    command itself (2026-10-03); the words after it, if any, are the name."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if line.startswith("/"):
        rest = _COMMAND.sub("", line, count=1).strip()
        while True:
            m = _FLAG.match(rest)
            if not m:
                break
            rest = rest[m.end():].strip()
        line = rest.lstrip("—–-:· ").strip()
    if not line:
        return ""
    # One sentence, cut at a word: a title, not the brief.
    first = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].rstrip(".")
    if len(first) > TITLE_CAP:
        first = first[:TITLE_CAP].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first[:1].upper() + first[1:]


def turn_session_title(turn, proposed: str = "") -> str:
    """What a session started by `turn` is called: the email's subject, else its
    schedule's name, else a readable form of what the runner proposed (or of the
    prompt), else "<Agent> turn". Never a bare command."""
    ref = turn.origin_ref or {}
    for key in ("subject", "schedule_name"):
        value = ref.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    named = readable_title(proposed) or readable_title(turn.prompt or "")
    if named:
        return named[:200]
    agent = turn.acting_agent
    return f"{agent.name} turn" if agent else "Turn"


def record_session(
    agent,
    thread_key: str,
    *,
    runner: Runner,
    project: str = "",
    workspace=None,
    session_key: str = "",
    session_id: str = "",  # accepted for wire-compat; the binding keys on session_key
    agent_task_ext_id: str | None = None,
    summary: str | None = None,
    title: str = "",
    turn=None,
):
    """Upsert the thread's durable Session + RunnerBinding and re-point the live-session
    hint at THIS runner/host. Only overwrites agent_task_ext_id/summary when passed,
    preserving accumulated context. The API caller has already gated the runner's
    owner against `workspace` — this stores, it does not authorize."""
    with transaction.atomic():
        binding = _binding_for_thread(agent, project, workspace, thread_key)
        if binding is None:
            session = _thread_session(agent, project, workspace, thread_key, runner=runner,
                                      turn=turn)
            binding = (
                RunnerBinding.objects.select_for_update()
                .filter(session=session)
                .first()
            )
            if binding is None:
                binding = RunnerBinding(session=session)
        binding.thread_key = thread_key
        binding.runner = runner
        binding.host = runner.host
        binding.session_key = session_key
        # The other half of the runner-side key (see RunnerBinding.emdash_project).
        # Mirrors what `Session.emdash_project` derives, from the arguments this
        # path already has: a project thread carries its repo, an agent thread its
        # agent's slug — which is the repo emdash actually runs it under.
        binding.emdash_project = project or (agent.slug if agent else "")
        # Name the row after the emdash task the human actually sees.
        # _thread_session titles a BRAND-NEW session with the raw thread_key,
        # which for an agent turn is an opaque hash (a real one leaked into the
        # Sessions list as "19f91250349ec91b"). Only retitle when the title is
        # still that fallback — never clobber a human-set chat title.
        # Retitle when the current title is NOT a name the human chose. Two
        # cases qualify: the raw thread_key fallback (an opaque hash for an agent
        # turn — one leaked into the Sessions list as "19f91250349ec91b"), and an
        # AUTOTITLE, which is the first user message truncated to 80 chars.
        #
        # The autotitle case is why this is more than an equality check: a
        # phone-created session gets autotitled before any emdash task exists, so
        # the old `title == thread_key` guard never matched and the session kept a
        # sentence for a name while the sidebar showed the task
        # (observed 2026-07-27). A human-set title is still never clobbered — it
        # won't match the fallback and won't match the first message either.
        #
        # A runner-supplied `title` beats the key: a cloud runner's key is a
        # Claude session UUID, which names nothing a person would recognise.
        # With no title, a UUID key is left for autotitle to name from the first
        # message rather than written as the title.
        from apps.canopy_sessions.autotitle import names_nothing

        name = (title or "").strip() or ("" if names_nothing(session_key) else session_key)
        if name and _title_is_derived(binding.session, thread_key):
            binding.session.title = name[:200]
            binding.session.save(update_fields=["title"])
        binding.live_seen_at = timezone.now()
        if agent_task_ext_id is not None:
            binding.agent_task_ext_id = agent_task_ext_id
        if summary is not None:
            binding.summary = summary
        binding.save()
        # A widget can attach before this binding exists — see seed_stream_desired.
        from apps.canopy_sessions.transcript_ingest import seed_stream_desired

        seed_stream_desired(binding)
        link_turn_session(turn or _turn_of_agent_thread(agent, thread_key, runner), binding.session)
    return binding


def _turn_of_agent_thread(agent, thread_key: str, runner: Runner):
    """The turn an AGENT thread key names, when it names one: both runners record
    an agent turn's session under `<agent>:<turn id>` (the laptop runner sends no
    `turn_id` with it). Only a turn of that agent this runner claimed; else None."""
    if agent is None:
        return None
    slug, _, tail = (thread_key or "").rpartition(":")
    if slug != agent.slug:
        return None
    try:
        turn_id = uuid.UUID(tail)
    except ValueError:
        return None
    return Turn.objects.filter(pk=turn_id, agent=agent, claimed_by=runner).first()


def link_turn_session(turn, session) -> bool:
    """Record that an agent/project `turn` ran in canopy `session`, so whoever
    dispatched it can follow up there with only the turn id (`TurnOut.
    canopy_session_id`). First write wins, like `stamp_turn_session`: a later
    record-session for a reused thread never moves a turn to another session.
    A chat turn is skipped — its `chat_session` already is its conversation.
    True if a row was linked."""
    if turn is None or session is None or turn.chat_session_id:
        return False
    linked = bool(
        Turn.objects.filter(pk=turn.pk, canopy_session__isnull=True).update(canopy_session=session)
    )
    if linked and turn.cli:
        # A follow-up into it runs on the CLI that holds its history (`clis.allowed`).
        Session.objects.filter(pk=session.pk, cli="").update(cli=turn.cli)
    return linked


def stamp_turn_session(turn_id, runner: Runner, session_key: str) -> bool:
    """Give a running turn its session key the moment its session exists.

    `finish` writes `session_key` too, but finish comes LAST — after the
    agent has already posted its close-out report, which joins on that key
    (apps/harness/closeout.py::_claim_dispatch_row). A laptop finishes seconds in
    (emdash runs the work after), so it never noticed; a cloud runner finishes
    when the work does, so every cloud close-out found no turn and became a
    second, report-only row (labs, echo, 2026-10-02 17:00).

    Only the runner that claimed the turn may stamp it, only while it runs, and
    only an empty key — the same first-write-wins rule `finish` keeps, so the
    two can never disagree. True if a row was stamped."""
    return bool(
        Turn.objects.filter(
            pk=turn_id, claimed_by=runner, session_key="",
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).update(session_key=session_key[:200])
    )


@transaction.atomic

def _title_is_derived(session, thread_key: str) -> bool:
    """True when a session's title was generated rather than chosen by a human.

    Generated titles are safe to replace with the emdash task name; a title
    somebody typed is not. Two generators exist: `_thread_session`'s raw
    thread_key fallback, and `autotitle.maybe_autotitle`, which takes the first
    user message, collapses whitespace and truncates to TITLE_MAX.
    """
    from apps.canopy_sessions.autotitle import TITLE_MAX

    title = (session.title or "").strip()
    if not title or title == thread_key:
        return True
    first = (
        Message.objects.filter(session=session, role=Message.USER)
        .order_by("turn_index")
        .values_list("plaintext", flat=True)
        .first()
    )
    if not first:
        return False
    return title == " ".join(first.split())[:TITLE_MAX]


def _reported_project(s) -> str:
    """The emdash PROJECT of a reported session, normalised to a string.

    getattr rather than attribute access for the same reason `agent_status` and
    `question` use it below: lightweight session objects also reach this service,
    and half of an identity must never be the reason a liveness report 500s.

    This is the value `Session.emdash_project` answers with on the way back out
    (`get_session_streams` ships it next to `session_key`), so a laptop-reported
    agent chat lines up: emdash runs it under the agent's own repo, which is the
    same string `emdash_project` derives from `agent.slug`.
    """
    return getattr(s, "project", "") or ""


#: `_agent_for_project` / `session_home`: the project names an agent OUTSIDE the
#: runner's tenant. Such a session is not filed anywhere (see `session_home`).
FOREIGN_AGENT = object()


def _session_tenant(runner) -> set[str]:
    """`runner_tenant_slugs(runner)`, read once per runner object: the report
    sweep asks `session_home` per session."""
    cached = getattr(runner, "_session_tenant_cache", None)
    if cached is None:
        cached = runner_tenant_slugs(runner)
        runner._session_tenant_cache = cached
    return cached


def _sensitive_box(runner) -> bool:
    """`is_sensitive_data_box(runner)`, read once per runner object, like the tenant."""
    cached = getattr(runner, "_sensitive_box_cache", None)
    if cached is None:
        cached = is_sensitive_data_box(runner)
        runner._sensitive_box_cache = cached
    return cached


def _agent_for_project(project: str, runner):
    """The agent a reported emdash PROJECT belongs to; None when it names no
    agent; `FOREIGN_AGENT` when it names an agent outside the runner's tenant.

    The wholesale sweep (`POST /runners/{id}/sessions`) reports every open task a
    runner can see and carries NO agent, so canopy had nothing to attribute the
    work with and fell back to the RUNNER's workspace. In practice every runner is
    registered in `dimagi` while ace/ada/echo/hal all live in `connect`, so every
    one of their sessions was filed under a tenant that does not contain the agent
    it is about — readable by that tenant's members, and invisible to any lister
    scoped to the agent's own. The feed also reported `agent: null` for all of
    them, which is the same defect seen from the other side.

    `project` IS the agent's repo by convention — the mapping
    `Session.emdash_project` already states in reverse ("an agent chat leaves
    project blank, but its worktree is still under the agent's own repo"). So a
    project naming an agent is that agent's work, and the tenant follows from the
    agent rather than from whichever machine happened to run it.

    None for a real repo checkout that is nobody's agent (canopy-web,
    connect-labs). Those keep the runner's workspace, which is right for them.

    An agent outside the RUNNER's tenant (`runner_tenant_slugs`) is neither the
    owner nor "nobody's": the box has that agent's work and no business filing it
    in either tree — not in the agent's (the box does not serve it), and not in
    its own as a plain project, where its members would read another tree's
    agent's transcript. `Agent.slug` is globally unique, so one row answers.
    """
    if not project:
        return None
    agent = Agent.objects.select_related("workspace").filter(slug=project).first()
    if agent is None:
        return None
    return agent if agent.workspace_id in _session_tenant(runner) else FOREIGN_AGENT


def session_home(runner, project: str, fallback=None):
    """(agent, project, workspace) a NEW agent-less session this box reports is filed
    under: the agent the project names, in its workspace (`_agent_for_project`);
    else the project, in the runner owner's default (`canopy_sessions.filing`),
    else `fallback` (the runner's workspace). The one rule, for the report sweep.

    None when the project names an agent outside the runner's tenant, or is a
    repo the runner's owner has not opted into (`Runner.opts_in`): such a
    session is not canopy's and is not filed at all (callers skip it). None, too,
    for anything a sensitive-data box reports: its sessions are canopy's own
    confined turns, and one canopy has no record of is a leftover it deletes,
    never a session to file and ask the box to ship."""
    if _sensitive_box(runner):
        return None
    owner = _agent_for_project(project, runner)
    if owner is FOREIGN_AGENT:
        return None
    if owner is not None:
        # A session targets an agent XOR a project (chat_session_not_agent_and_project).
        return owner, "", owner.workspace
    if not runner.opts_in(project):
        return None  # plain Claude Code: the owner did not opt this repo into canopy
    from apps.canopy_sessions.filing import filing_workspace

    return None, project, filing_workspace(runner, fallback if fallback is not None else runner.workspace,
                                           tenant=_session_tenant(runner))


def report_projects(runner) -> list[str] | None:
    """The repos whose sessions this box should report at all — sent on its
    heartbeat reply, so the box filters BEFORE reading any tail and nothing from a
    plain-Claude-Code repo leaves it. None = every repo (the owner opted into
    everything). Else the opted-in repos plus every repo the box holds that is an
    agent's own in its tenant: exactly what `session_home` would file."""
    opt_in = ProjectOptIn.for_user_id(runner.owner_id) if runner.owner_id else None
    if opt_in is not None and opt_in.everything:
        return None
    wanted = set(opt_in.projects or []) if opt_in is not None else set()
    for project in runner.observed_projects():
        owner = _agent_for_project(project, runner)
        if owner is not None and owner is not FOREIGN_AGENT:
            wanted.add(project)
    return sorted(wanted)


def fire_sessions_closed(session_ids: list) -> None:
    from apps.harness.signals import sessions_closed

    try:
        sessions_closed.send(sender=Session, session_ids=list(session_ids))
    except Exception:  # noqa: BLE001 — a notifier must never fail the report
        logger.exception("sessions_closed receivers failed")


# What a dialog IS, as opposed to how it was last seen: its question and the
# options offered. `observed_at` is re-stamped on every read, `selected` follows
# the cursor, `answer_*` report on a tap, and `title`/`body`/`source` differ
# between the hook, transcript and screen producers for the SAME dialog — none
# of those make it a different question. Mirrors `apps.slack.menus.content_key`,
# which dedupes the Slack post the same way.
def _ask_identity(menu):
    if not menu:
        return None
    # A sensitive conversation's dialog keeps no words (`sensitivity.menu_metadata`),
    # only their digest, which is what tells one ask from the next.
    return (str(menu.get("question") or "") + str(menu.get("sha256") or ""),
            tuple(str(o.get("label") or "") for o in (menu.get("options") or [])
                  if isinstance(o, dict)))


def _unlinked_chat_binding(runner: Runner, s, project: str):
    """A binding for a reported task that a CHAT turn of this runner created but
    never linked, or None.

    The runner creates the emdash task and then links it with record-session; when
    that call fails (a 500 mid-deploy) the task is first seen here, unbound, and
    used to be filed as a session of its own beside the chat it belongs to (Slack
    turn f7cb8694, 2026-10-23). The turn names its task (`Turn.session_key`, from
    its finish), so the task is bound to that turn's chat instead. Only a chat this
    runner's turn ran in, whose worktree project is the task's, and that is not
    already bound to a task — never a second conversation's."""
    turn = (Turn.objects.select_related("chat_session")
            .filter(claimed_by=runner, session_key=s.emdash_task, chat_session__isnull=False)
            .order_by("-created_at").first())
    if turn is None:
        return None
    session = turn.chat_session
    if session.emdash_project != project:
        return None
    binding = (RunnerBinding.objects.select_for_update(of=("self",))
               .filter(session=session).first())
    if binding is not None and binding.session_key:
        return None
    if binding is None:
        binding = RunnerBinding(session=session)
    binding.session_key = s.emdash_task
    binding.emdash_project = project
    binding.thread_key = str(session.pk)
    binding.host = runner.host
    binding.task_uid = str(getattr(s, "task_uid", "") or "")[:100]
    logger.info("runner %s reported unlinked task %r; bound to chat session %s (turn %s)",
                runner.name, s.emdash_task, session.pk, turn.pk)
    return binding


def replace_reported_sessions(
    runner: Runner, workspace, sessions: list, archived: list[str] | None = None,
    complete: bool = False, skipped: list | None = None,
) -> int:
    """Upsert a durable Session(origin=runner) + RunnerBinding per reported
    session. Sessions that fell off the report keep their Session row but have
    their live binding cleared.

    `archived` is the CLOSING signal — emdash task names this runner has seen
    archived. Absence from `sessions` is ambiguous (archived? runner down?
    truncated?), so it can never retire a row on its own; an explicit name here can.
    Scoped to THIS runner's bindings, because a task name is not unique across
    machines and one laptop must never retire another's session.

    The SAME binding this writes doubles as the reuse target for a phone-
    dispatched "Continue" turn (`origin_ref.thread_key = "emdash:<task>"`,
    e.g. `OpenSessions.tsx`) — `thread_key` + `host` are stamped ONLY when the
    binding is freshly created here, so `_binding_for_thread` can find a
    project-Continue row that has no other origin. Pre-fold (SessionLink era)
    a SECOND row existed purely for that lookup; now there is only one row,
    but an existing binding's durable identity (thread_key/host) is left
    untouched on update — the runner's ambient sweep reports EVERY open
    emdash task (agent- or project-driven, no filter), so a session already
    bound by `record_session` to an agent/phone thread must not have that
    binding's thread_key silently reassigned to `emdash:<task>` underneath it
    (that would orphan the agent thread's reuse lookup and fork a duplicate
    session on its next turn)."""
    # Record that this runner PARTICIPATES in the wholesale report — the observer
    # session staleness is derived against (see Runner.sessions_reported_at and
    # canopy_sessions.staleness.unseen_q). Stamped BEFORE the early-outs and
    # regardless of whether `sessions` is empty: an empty report is the case that
    # must retire everything on the box, so it is exactly the one that has to count
    # as having reported. A targeted one-column `update()`, never `runner.save()` —
    # this service is called on a ~10s cadence and a full save here would join the
    # bug class where a frequent writer silently clears fields another writer owns
    # (`code_sha`/`code_branch` have each paid for that once).
    Runner.objects.filter(pk=runner.pk).update(sessions_reported_at=timezone.now())

    # emdash task NAMES are not unique — two un-archived tasks can share a name
    # (see task_state's "Names aren't unique in emdash's schema" note). Collapse
    # duplicates before upserting; the runner sends newest-first, so the first
    # occurrence is the live session and an older namesake is stale and correctly
    # dropped (observed 2026-07-20 with two "mobile" tasks).
    #
    # Keyed on (PROJECT, name), not the bare name. A name is only reused-and-stale
    # within ONE project; the same name in two projects is two live conversations,
    # and collapsing those dropped the second from the report ENTIRELY — no row, no
    # error, invisible on the web. That is the more expensive half of the collision
    # this key fixes, because a mislabelled session is at least visible.
    deduped, seen = [], set()
    for s in sessions:
        ident = (_reported_project(s), s.emdash_task)
        if ident in seen:
            continue
        seen.add(ident)
        deduped.append(s)

    now_keys = {s.emdash_task for s in deduped}
    # The bindings this report actually touched. Identity by ROW, not by name: the
    # reconciliation below (un-archive, stale menus, unsatisfied closes) has to say
    # "this exact session was in the report", and `session_key__in=now_keys` cannot
    # — it would revive a project's archived namesake because a DIFFERENT project's
    # task of the same name is alive.
    touched_ids: list = []
    # (session_id, menu|None) for every session whose dialog appeared or went
    # away in THIS report — pushed after commit so an open chat updates without
    # a reload. Only the edges: the report repeats every ~10s and republishing
    # an unchanged menu would re-render the buttons under a thumb.
    menu_changes: list[tuple] = []
    new_asks: list[tuple] = []
    # Tasks of another tree's agent (`session_home` → None): never filed. Their
    # names go to `skipped` when the caller passed a list; each project is logged once.
    foreign_logged: set = set()
    # Where a NEW session is filed is `session_home` — consulted only when one is
    # created (the steady-state report creates nothing), never for an EXISTING
    # session, which is what keeps a session someone moved where they put it.

    # The loop takes select_for_update locks, which Django REJECTS outside a
    # transaction — "select_for_update cannot be used outside of a transaction".
    # That was latent from #350: nothing in the loop wrote after taking the lock,
    # so the error was never raised. Adding a `.save()` (the title repair) made it
    # fire, 500ing the endpoint the runner calls every ~10s to report which
    # sessions are alive — the whole machine went dark on liveness.
    #
    # Wrapping the loop is the real fix: the lock was always meant to serialize
    # concurrent reports for a task, and without a transaction it never did.
    with transaction.atomic():
        # Every candidate binding for this report, locked and loaded in ONE query
        # with its session, instead of up to two locked lookups plus a lazy
        # session load per reported task — that loop was ~8 queries a session,
        # ~160 a report, from every box every ~10s, all on the one process that
        # also serves every page (SLOW_REQUEST, 2026-10-03). Same predicate as the
        # per-task lookup below, keyed by (task, project) for the same reason.
        candidates: dict[tuple[str, str], RunnerBinding] = {}
        from apps.canopy_sessions.handoff import suppressed_tasks  # noqa: PLC0415 — cycle: handoff -> harness.services -> channels.email -> session_sync

        handed_off = suppressed_tasks(runner) if deduped else set()
        if deduped:
            host_match_all = Q(runner__isnull=True) & Q(host=runner.host) & ~Q(host="")
            for row in (RunnerBinding.objects.select_for_update(of=("self",))
                        .select_related("session")
                        .filter(session_key__in=[x.emdash_task for x in deduped])
                        .filter(Q(runner=runner) | host_match_all)
                        .order_by("pk")):
                candidates.setdefault((row.session_key, row.emdash_project), row)
        for s in deduped:
            # Find this runner's binding for the task WITHOUT depending on the live
            # `runner` FK — the clear step below nulls it for anything that fell off the
            # report, and a lookup keyed on it would then miss the row and fork a
            # DUPLICATE Session when the task reappears. The two branches are
            # asymmetric on purpose: `runner=runner` preserves today's behaviour
            # exactly while the FK is set (legacy bindings carry host="" and would stop
            # matching if we keyed on host alone), and the null branch recovers a row
            # THIS runner previously released — scoped by host, because emdash task
            # names collide across machines and one laptop must never claim another's.
            # The null branch requires a NON-BLANK host: a legacy binding with host=""
            # would otherwise be recoverable by any runner whose own host is "" (two
            # un-heartbeated runners would fuse). `runner=runner` still covers a
            # host="" binding this runner currently owns, so that case is unaffected.
            project = _reported_project(s)
            # The project is HALF THE KEY, not a detail carried alongside it (see
            # RunnerBinding.emdash_project). Matching on the name alone fused two
            # repos' same-named tasks into one row.
            #
            # The second lookup is the legacy branch, and it is what makes this
            # deploy a no-op for every row that already exists: a binding written
            # before 0021 backfilled the column — or by a runner that reports no
            # project at all — carries a blank, and must still be recognised and
            # then FILLED, exactly as `host` and `thread_key` are below. It cannot
            # re-open the hole it closes: a blank is adopted once and is no longer
            # blank, and a binding carrying a DIFFERENT project is never matched.
            binding = candidates.get((s.emdash_task, project))
            if binding is None and project:
                binding = candidates.pop((s.emdash_task, ""), None)
            if binding is None and (s.emdash_task, project) in handed_off:
                # A task this box transferred away and has not closed yet (it
                # closes once the target restores the work, `handoff.py`). Filing
                # it would make a ghost of the session that moved.
                continue
            if binding is None:
                binding = _unlinked_chat_binding(runner, s, project)
            if binding is None:
                # Tenant AND agent follow the project when it names an agent —
                # not the runner. A runner is a machine serving several agents
                # across tenants, so "whose work is this" is a question its own
                # workspace cannot answer.
                filed = session_home(runner, project, workspace)
                if filed is None:
                    # Another tree's agent's work, or a repo the owner did not opt
                    # into: filed nowhere, and said once.
                    if skipped is not None:
                        skipped.append(s.emdash_task)
                    if project not in foreign_logged:
                        foreign_logged.add(project)
                        logger.info(
                            "runner %s reported a session in %r, which is not canopy's "
                            "(another tenant's agent, or not opted in); not filed",
                            runner.name, project)
                    continue
                owner, filed_project, home = filed
                session = Session.objects.create(
                    agent=owner,
                    # A session targets an agent XOR a project
                    # (chat_session_not_agent_and_project), so the project is
                    # cleared when an agent owns it. Nothing is lost:
                    # `Session.emdash_project` returns the agent's slug in that
                    # case, so the (project, task) pair the runner resolves a
                    # transcript by is byte-identical either way — which is also
                    # why RunnerBinding.emdash_project, the key the 10s report
                    # loop reuses on, does not move.
                    project=filed_project,
                    # An agent's project follows the agent; anything else is
                    # filed per the runner owner's default (canopy_sessions.filing).
                    workspace=home,
                    origin=Session.ORIGIN_RUNNER,
                    title=s.emdash_task,
                )
                binding = RunnerBinding(session=session, session_key=s.emdash_task)
                binding.emdash_project = project
                binding.thread_key = f"emdash:{s.emdash_task}"
                binding.host = runner.host
                binding.task_uid = str(getattr(s, "task_uid", "") or "")[:100]
            else:
                # The NAME matched; is it the same TASK? A new emdash task that reused
                # a name gets its own session here, and the old record keeps its rows
                # (board task T74). getattr: lightweight session objects reach this
                # service too, and an old runner sends no id — which forks nothing.
                from apps.canopy_sessions.transcript_ingest import fork_if_task_changed

                fork_if_task_changed(binding, str(getattr(s, "task_uid", "") or "")[:100])
                # Correct a GENERATED title on an existing session. A brand-new
                # session above is titled from the emdash task, but an existing one
                # never was — so a session created on the phone kept its autotitle
                # (the first user message, truncated) forever, while emdash's own
                # sidebar showed the task name. The first fix went into
                # `record_session`, which only runs when a TURN is routed; this is
                # the path that runs every ~10s, which is why the repair never
                # actually happened (observed 2026-07-27, after shipping it).
                #
                # `_title_is_derived` recognises only titles WE generated, so a title
                # a human chose is still never touched.
                # Cosmetic repair, kept isolated: a title must never cost liveness.
                # The enclosing `transaction.atomic()` (see below) is what makes the
                # select_for_update above legal at all; this inner block only stops a
                # retitle failure from rolling the whole report back.
                # The cheap comparison first: in the steady state the title already
                # IS the task name, and `_title_is_derived` reads a message to decide.
                try:
                    if binding.session.title != s.emdash_task[:200]:
                        with transaction.atomic():
                            if _title_is_derived(binding.session, binding.thread_key or ""):
                                binding.session.title = s.emdash_task[:200]
                                binding.session.save(update_fields=["title"])
                except Exception:  # noqa: BLE001 — a title must never cost liveness
                    logger.warning("could not retitle session %s from task %r",
                                   binding.session_id, s.emdash_task, exc_info=True)
                # thread_key/host are the binding's durable IDENTITY. NEVER overwrite a
                # non-empty one — an existing binding may be owned by an agent/phone
                # thread (record_session) and this report loop must not steal it (see
                # the docstring above). But DO fill an EMPTY one: bindings predating the
                # SessionLink fold have host="" and can never satisfy
                # RunnerBinding.reusable_by (which requires runner AND host), so a chat
                # sent to one spawned a fresh emdash session forever instead of reusing
                # the live one. Fill-if-empty heals those without clobbering anything.
                if not binding.thread_key:
                    binding.thread_key = f"emdash:{s.emdash_task}"
                if not binding.host:
                    binding.host = runner.host
                # Fill the legacy blank matched above, so the row is keyed properly
                # from here on. Same fill-if-empty shape, and same reason.
                if not binding.emdash_project:
                    binding.emdash_project = project
                # And repair the LABEL on the session itself. `project` was only
                # ever written at creation, so a row that got the wrong one — from
                # the name-only fusion this change removes, or from a blank adopted
                # just above — displayed it forever with nothing to correct it.
                # Guarded on `agent`: an agent chat identifies by its agent and
                # carries a deliberately blank `project` (the model's own
                # chat_session_not_agent_and_project constraint forbids both), so
                # writing one here would violate it.
                #
                # Cosmetic, so isolated like the retitle above: a label must never
                # cost liveness.
                try:
                    sess = binding.session
                    if project and sess.agent_id is None and sess.project != project:
                        with transaction.atomic():
                            sess.project = project
                            sess.save(update_fields=["project"])
                except Exception:  # noqa: BLE001 — a label must never cost liveness
                    logger.warning("could not repair project on session %s to %r",
                                   binding.session_id, project, exc_info=True)
            binding.runner = runner
            binding.status = s.status or ""
            # The engine's own liveness flag, written through EVERY report including
            # blank — the same reasoning as `pending_question` below: this is a fresh
            # observation, so "it stopped working" has to be able to clear "working",
            # or a finished session keeps a live badge forever. getattr for the same
            # reason too: lightweight session objects also reach this service, and a
            # display flag must never be why a liveness report fails.
            binding.agent_status = getattr(s, "agent_status", "") or ""
            # Written through on EVERY report including False, for the same reason
            # as the flag it qualifies: it is a fresh observation, so "it has gone
            # quiet" has to be able to clear "it is still writing".
            binding.agent_status_stale = bool(getattr(s, "agent_status_stale", False))
            binding.last_interacted_at = _aware(s.last_interacted_at)
            binding.live_seen_at = timezone.now()
            # The one write site. See RunnerBinding.reported_at — `close` branches
            # on this, and it is only trustworthy because record_session leaves it
            # alone.
            binding.reported_at = binding.live_seen_at
            from . import sensitivity

            # A sensitive conversation's screen never comes to rest here.
            binding.tail = ([] if sensitivity.is_sensitive(binding.session)
                            else list(s.recent_messages or []))
            # Written unconditionally, INCLUDING None. The report is a fresh
            # observation of the session's screen every ~10s, so "no dialog" has
            # to be able to clear one — otherwise a menu answered at the laptop
            # keeps live buttons on every phone that opens the session, and a
            # tap then presses a number at a prompt that is no longer a dialog.
            was_asking = binding.pending_question or None
            # getattr, not attribute access: this service is also called
            # directly with lightweight session objects, and a dialog must
            # never be the reason a liveness report fails.
            binding.pending_question = getattr(s, "question", None) or None
            # A sensitive conversation's dialog is kept as metadata: the question
            # and its options are the host's data as often as not.
            if binding.pending_question and sensitivity.is_sensitive(binding.session):
                binding.pending_question = sensitivity.menu_metadata(binding.pending_question)
            if was_asking != binding.pending_question:
                menu_changes.append((binding.session_id, binding.pending_question))
                # Whether this is a NEW ask, not the same one seen again. Every
                # producer re-stamps `observed_at` on each read, so the raw dicts
                # differ on every ~10s report for as long as a dialog is up —
                # comparing them is what buzzed a phone every ten seconds
                # (2026-09-22). The frame above still goes out (it carries the
                # fresh stamp); the push is for the ask itself.
                if binding.pending_question and (
                        _ask_identity(was_asking) != _ask_identity(binding.pending_question)):
                    new_asks.append((binding.session_id, binding.pending_question))
            binding.save()
            # The same race as record_session: a viewer may have attached before
            # this report created the binding.
            from apps.canopy_sessions.transcript_ingest import seed_stream_desired

            seed_stream_desired(binding)
            touched_ids.append(binding.pk)

    # Un-archive anything re-reported as open. The DERIVED staleness half of
    # `state=active` recomputes on every read, but this WRITTEN half does not heal
    # itself — without this, a task you reopened in emdash stays archived forever.
    #
    # A revival is also flagged, because the report cannot tell a REOPENED task
    # from a new task that reused a closed one's name — only the transcript the
    # next ship carries can (canopy_sessions.services.fork_if_name_reused).
    if touched_ids:
        from apps.canopy_sessions.transcript_ingest import REOPENED_KEY

        with transaction.atomic():
            for revived in Session.objects.select_for_update().filter(
                runner_binding__id__in=touched_ids, status=Session.ARCHIVED,
            ):
                revived.metadata = {**(revived.metadata or {}), REOPENED_KEY: True}
                revived.status = Session.ACTIVE
                revived.save(update_fields=["metadata", "status", "updated_at"])

    # Apply the closing signal. `now_keys` wins over `archived`: emdash task names are
    # not unique, so an open task must never be retired by an archived namesake.
    #
    # Still keyed on the NAME, unlike everything above, because that is all the wire
    # carries — `ReportSessionsIn.archived` is a list of strings with no project. So
    # the name-level guard stays: an archived `issues` under one repo cannot retire a
    # live `issues` under another, at the cost of not retiring it either. Erring
    # towards leaving a row open is the safe direction (staleness retires it on its
    # own clock); erring the other way deletes a live session from the web.
    closed = [k for k in (archived or []) if k and k not in now_keys]
    newly_closed: list = []
    if closed:
        closing = Session.objects.filter(
            runner_binding__runner=runner,
            runner_binding__session_key__in=closed,
        ).exclude(status=Session.ARCHIVED)
        newly_closed += list(closing.values_list("id", flat=True))
        closing.update(status=Session.ARCHIVED)

    # The other closing signal: absence from a COMPLETE report. emdash deletes a
    # task it closes (no row, no flag), so for an ordinary close the report's
    # silence is all there is — but it is a real observation when the report
    # arrived and held the runner's whole open set: a runner that cannot read
    # emdash sends no report at all, and a laptop asleep sends nothing, so
    # neither can reach this. Two complete reports in a row, to absorb a
    # flickering read. A wrong call heals itself: a task that is reported again
    # is un-archived above on the very next report.
    RunnerBinding.objects.filter(id__in=touched_ids).exclude(missed_reports=0).update(missed_reports=0)
    if complete:
        missing = (RunnerBinding.objects.filter(runner=runner)
                   .exclude(session_key="").exclude(id__in=touched_ids)
                   .exclude(session__status=Session.ARCHIVED))
        missing.update(missed_reports=F("missed_reports") + 1)
        gone = Session.objects.filter(runner_binding__in=missing.filter(missed_reports__gte=2))
        newly_closed += list(gone.values_list("id", flat=True))
        gone.update(status=Session.ARCHIVED)

    # A menu, unlike the runner FK below, MUST be cleared when its session stops
    # being reported. `pending_question` is only written inside the loop above,
    # which walks the sessions IN this report — so a session whose emdash task is
    # gone keeps its last dialog forever. Observed 2026-08-01: `7891` was absent
    # from emdash's open set entirely and the API still served a phone six
    # buttons, every one of which could only fail. The runner cannot fix this
    # from its side: it prunes its own copy, but it has no way to say anything
    # about a session it can no longer see.
    #
    # This is the same wholesale reconciliation the report already performs for
    # liveness — absence is a direct observation, not an inference — applied to
    # the one other field a report owns. It is NOT the same judgement as the FK
    # below: identity is durable and answers "which box", while a dialog is a
    # claim about a screen that no longer exists.
    # A close is satisfied by the task being gone, which is exactly what absence
    # from this wholesale report means. Clearing it here (rather than on a runner
    # ack) keeps one source of truth: the report already decides what is open.
    RunnerBinding.objects.filter(runner=runner, close_requested=True).exclude(
        id__in=touched_ids).update(close_requested=False)
    # ...and so is a FAILED close: the task is gone, whoever removed it, so the
    # "Close failed: …" a runner reported no longer describes anything.
    RunnerBinding.objects.filter(runner=runner).exclude(close_error="").exclude(
        id__in=touched_ids).update(close_error="", close_error_at=None)

    stale_menus = RunnerBinding.objects.filter(runner=runner).exclude(
        pending_question__isnull=True,
    ).exclude(id__in=touched_ids)
    for binding in stale_menus:
        menu_changes.append((binding.session_id, None))
    stale_menus.update(pending_question=None)

    # NOTHING is cleared here. `RunnerBinding.runner` is durable IDENTITY — which box
    # this session lives on — and a session must never forget that just because its
    # task stopped being reported (emdash DELETES a closed task, so falling off the
    # report is the NORMAL end of life, not an anomaly). Liveness is `live_seen_at`,
    # stamped above on everything in this report and read against
    # SESSION_LIVE_WINDOW; see apps/canopy_sessions/staleness.py.
    #
    # Nulling the FK here is what left labs with 47 sessions that were listed as
    # active, could not say which runner they came from, and had no way back.

    # Fire AFTER commit so apps/realtime fans the durable rows (never racing the DB)
    # to the runner-owner's supervisor group — the WS push that makes live emdash
    # activity reach every connected viewer at once. Local import avoids a cycle.
    def _fire_reported():
        from apps.harness.signals import sessions_reported

        sessions_reported.send(sender=Runner, runner=runner)
        _publish_menu_changes(menu_changes, new_asks)

    transaction.on_commit(_fire_reported)
    if newly_closed:
        transaction.on_commit(lambda: fire_sessions_closed(newly_closed))
    return len(deduped)


def _publish_menu_changes(menu_changes: list[tuple], new_asks: list[tuple]) -> None:
    """Tell everyone a session's dialog changed. Called after commit by both
    producers: the laptop's wholesale report and a cloud box's `report_session_menu`."""
    # The dialog, to anyone already looking at that chat. Its own frame
    # rather than an overloaded `session.activity`: activity is "is the
    # agent producing", which the hook path owns and answers within a
    # tick — inventing a state here to carry a menu would mean reporting
    # an agent as idle or blocked on this path's much slower clock.
    from apps.realtime import groups

    for session_id, menu in menu_changes:
        groups.publish(groups.session_group(session_id),
                       {"type": "session.menu", "menu": menu})

    from apps.harness.signals import session_menu_changed

    # `new_ask`: the agent just STARTED waiting (apps/push buzzes the phone).
    asking = {session_id for session_id, _ in new_asks}
    for session_id, menu in menu_changes:
        try:
            session_menu_changed.send(sender=RunnerBinding, session_id=session_id, menu=menu,
                                      new_ask=session_id in asking)
        except Exception:  # noqa: BLE001 — a consumer must not break the report
            logger.exception("session_menu_changed receiver failed")


def end_turns_ask(turn: Turn) -> None:
    """A finished chat turn retires the ACP ask it was holding open.

    A cloud box's ask lives exactly as long as the turn: the agent is blocked
    inside it. When the box clears it itself this is a no-op; when the box died
    mid-ask it is the only signal left, since a cloud box makes no wholesale
    report to retract it. A laptop's menu (any other `source`) is what its
    terminal shows, which outlives a turn — only its report clears that.
    """
    if not turn.claimed_by_id:
        return
    binding = (RunnerBinding.objects.select_related("runner")
               .filter(session_id=turn.chat_session_id, runner_id=turn.claimed_by_id).first())
    if binding is not None and (binding.pending_question or {}).get("source") == "acp":
        report_session_menu(binding.runner, binding.session_id, None)


#: The keys a subagent summary may carry (`canopy_transcript.summarize_subagents`),
#: and the ones a sensitive conversation keeps: that work is happening and how
#: much, never what it is pointed at.
SUBAGENT_KEYS = ("agent_id", "tool_use_id", "parent_agent_id", "description", "agent_type",
                 "background", "state", "steps", "last", "started_at", "updated_at")
_SUBAGENT_WORDS = ("description", "last")
SUBAGENTS_MAX = 50


def report_session_subagents(runner: Runner, session_id, subagents: list[dict]) -> bool:
    """What the subagents of ONE session on this runner are doing, wholesale.

    Both runners send it from the loop that already tails every session they
    back, whenever a summary changes. Stored on the binding and pushed to the
    chat as `session.subagents` only when it differs. False when this runner
    does not hold the session: one box never writes onto another's.
    """
    binding = RunnerBinding.objects.filter(session_id=session_id, runner=runner).first()
    if binding is None:
        return False
    from . import sensitivity  # noqa: PLC0415 — as report_session_menu below

    hidden = sensitivity.is_sensitive(binding.session)
    clean = []
    for s in (subagents or [])[-SUBAGENTS_MAX:]:
        if not isinstance(s, dict) or not s.get("agent_id"):
            continue
        row = {k: s.get(k) for k in SUBAGENT_KEYS}
        if hidden:
            row.update({k: "" for k in _SUBAGENT_WORDS})
        clean.append(row)
    if clean == (binding.subagents or []):
        return True
    binding.subagents = clean
    binding.save(update_fields=["subagents"])

    from apps.realtime import groups  # noqa: PLC0415 — as _publish_menu_changes: realtime imports harness

    transaction.on_commit(lambda: groups.publish(
        groups.session_group(binding.session_id),
        {"type": "session.subagents", "subagents": clean}))
    return True


def report_session_menu(runner: Runner, session_id, menu: dict | None) -> bool:
    """A cloud box's dialog for ONE session it holds: set it, or clear it (None).

    The laptop says the same thing inside its wholesale emdash report; a cloud
    box makes no such report — it holds the agent's ask open over ACP, so it
    knows exactly when one starts and ends, and says so per session. Same field,
    same frames, same once-per-ask push. False when this runner does not hold
    the session: one box never writes a dialog onto another's.
    """
    binding = RunnerBinding.objects.filter(session_id=session_id, runner=runner).first()
    if binding is None:
        return False
    was_asking = binding.pending_question or None
    binding.pending_question = menu or None
    if binding.pending_question:
        from . import sensitivity  # noqa: PLC0415 — as in the laptop's report above

        # As the laptop's report above: a sensitive conversation's dialog is metadata.
        if sensitivity.is_sensitive(binding.session):
            binding.pending_question = sensitivity.menu_metadata(binding.pending_question)
    if was_asking == binding.pending_question:
        return True
    binding.save(update_fields=["pending_question"])
    changes = [(binding.session_id, binding.pending_question)]
    asks = ([(binding.session_id, binding.pending_question)]
            if binding.pending_question
            and _ask_identity(was_asking) != _ask_identity(binding.pending_question) else [])
    transaction.on_commit(lambda: _publish_menu_changes(changes, asks))
    return True


@dataclass
class SessionView:
    """The wire projection of a live runner session — the fields EmdashSessionOut
    reads. Derived from Session + RunnerBinding; preserves the frozen shape."""

    id: str
    emdash_task: str
    project: str
    agent: str | None
    status: str
    last_interacted_at: object
    recent_messages: list
    workspace_id: str
    runner_name: str


def list_visible_sessions(user, ws_slugs: set[str] | None = None) -> list[SessionView]:
    """Open sessions in the caller's workspaces whose runner is LIVE. Newest-first.

    Two conditions, all polled or explicit — none of them "the FK went null":
      * the session is open by `staleness.active`, the ONE rule the session list
        and the runner row's `open_sessions` count also read (not archived; still
        reported, or on the cloud runner not yet finished),
      * and its runner is still heartbeating (Runner.live_status).
    They overlap by design: a runner that stops heartbeating also stops
    reporting, so the strictest of the two wins and a dead box's rows go quiet fast.

    A domain-matching teammate who has not been let into the workspace (an
    invite, or an approved access request) has no WorkspaceMembership row, so
    `user_workspace_slugs(user)` returns empty and their workspace's sessions
    are correctly invisible to them — no more silent auto-join here either.

    `ws_slugs` narrows to a request's read scope (`GET /api/harness/sessions`
    passes `wsvc.request_workspace_slugs`, so a caller in two tenant trees must
    pin one). Omitted — the realtime push to the runner owner's own socket —
    it is every membership.
    """
    from apps.canopy_sessions import access as session_access

    if ws_slugs is None:
        ws_slugs = wsvc.user_workspace_slugs(user)
    bindings = (
        RunnerBinding.objects.filter(
            runner__isnull=False,
            session__workspace_id__in=ws_slugs,
            # The chat ACL, not just the tenant: this returns each session's
            # recent messages.
            session__in=active(
                Session.objects.filter(session_access.visible_session_q(user))
            ).values("pk"),
        )
        .select_related("runner", "session", "session__agent")
        .order_by("-last_interacted_at")
    )
    out = []
    for b in bindings:
        if b.runner.live_status != Runner.ONLINE:
            continue
        out.append(
            SessionView(
                id=str(b.session_id),
                emdash_task=b.session_key,
                # `emdash_project`, not the raw column: a session targets an
                # agent XOR a project, so an agent-owned row has project="" and
                # reading it directly answered "" for every ACE run. The derived
                # value is the one that never moved — and it is the same half of
                # the identity a runner resolves a transcript by.
                project=b.session.emdash_project,
                agent=(b.session.agent.slug if b.session.agent_id else None),
                status=b.status,
                last_interacted_at=b.last_interacted_at,
                recent_messages=b.tail,
                workspace_id=b.session.workspace_id,
                runner_name=b.runner.name,
            )
        )
    return out


# Session-stream kinds that are STATE, not transcript: they fan out to watching
# clients and are never persisted (index -1 already excludes them from the durable
# write, and they have no transcript row to be).
#
#   activity:  is the agent producing right now — working | idle | blocked
#   stop:      did a stop the human asked for actually land — requested | stopped
#              | failed. A SEPARATE axis from activity on purpose: after a stop
#              that did not take, the agent is still `working`, and that is true
#              and must stay true. Folding "the stop failed" into activity would
#              either lie about what the agent is doing or lose the stop entirely.
LIVE_ONLY_PREFIXES = ("activity:", "stop:")


def ingest_session_stream(binding, *, transcript_id, events) -> int:
    """Live conversational events a runner ships for a session it backs. For a
    transcript-sourced session, events carrying a transcript ordinal are
    PERSISTED as Message rows first (the transcript is the durable source), then
    — only while someone is watching (`binding.stream_desired`) — fanned out to
    the session group as the chat path's `chat.turn_event` frames. Returns how
    many frames were pushed. The route (`post_session_stream`) only finds the
    binding."""
    return _ingest(binding.session, transcript_id=transcript_id, events=events,
                   index_offset=binding.index_offset, live=binding.stream_desired,
                   binding=binding)


def ingest_turn_end_rows(session, *, transcript_id, events) -> int:
    """A CONFINED turn's transcript rows, shipped by the runner that ran it: live
    while it runs (the runner tails the turn's own transcript, `_TurnTail` in
    runner/ec2/cloud_runner.py) and once more, whole, when it ends. A confined turn
    is never recorded as the session's binding, so this is the one way its reply
    reaches the conversation. Every ship of one transcript lands in the same epoch,
    so a row is kept once and fanned out once — the first time it arrives — and the
    turn-end ship is a no-op for every row the live tail already shipped. A confined turn that starts a
    fresh Claude session begins again at ordinal 0, so each `transcript_id` gets its
    own epoch above the rows already held (`_turn_end_epoch`). A turn of a sensitive
    conversation that resumed the session its box kept ships the same transcript
    again, under the same id: the earlier turns' rows land on the rows already held
    and are skipped, and only its own are added; a re-ship of the same turn is a
    no-op. Always fanned out: the person who asked is waiting for it."""
    offset = _turn_end_epoch(session, transcript_id)
    shifted = [e.model_copy(update={"index": e.index + offset}) if e.index >= 0 else e
               for e in events]
    return _ingest(session, transcript_id="", events=shifted, index_offset=0, live=True,
                   binding=None, fan_out_held=False)


#: Session metadata: the epoch (`turn_index` offset) of each confined turn's
#: transcript that has shipped into it, by Claude session id. Server-owned.
TURN_END_EPOCHS = "turn_end_epochs"


def _turn_end_epoch(session, transcript_id: str) -> int:
    """Where this transcript's ordinals start in `session`: the offset it was given
    the first time it shipped, else one clear stride above every row held — the
    rule a bound session's new file follows (`transcript_ingest._open_epoch`)."""
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        epochs = dict((locked.metadata or {}).get(TURN_END_EPOCHS) or {})
        key = transcript_id or "-"
        if key in epochs:
            return int(epochs[key])
        high = Message.objects.filter(session=locked).aggregate(m=Max("turn_index"))["m"]
        offset = 0 if high is None else ((int(high) // BLOCK_STRIDE) + 2) * BLOCK_STRIDE
        epochs[key] = offset
        locked.metadata = {**(locked.metadata or {}), TURN_END_EPOCHS: epochs}
        locked.save(update_fields=["metadata", "updated_at"])
    session.metadata = locked.metadata
    return offset


def turn_end_held(session) -> dict[str, int]:
    """{transcript id: how many of its records `session` holds rows through} for every
    confined turn's transcript that has shipped into it (`TURN_END_EPOCHS`): one past
    the record of its highest row. A runner resuming that transcript ships from
    here, not from what it believes it shipped before — a ship that failed then is
    sent again, never assumed to have landed. Each transcript's rows are the ones in
    its epoch, up to the next epoch's start."""
    epochs = sorted((int(off), key) for key, off in
                    ((session.metadata or {}).get(TURN_END_EPOCHS) or {}).items() if key != "-")
    if not epochs:
        return {}
    bounds = [(key, off, nxt) for (off, key), nxt in
              zip(epochs, [o for o, _ in epochs[1:]] + [None], strict=True)]
    highs = Message.objects.filter(session=session).aggregate(**{
        f"e{i}": Max("turn_index", filter=Q(turn_index__gte=off) & (
            Q(turn_index__lt=nxt) if nxt is not None else Q()))
        for i, (_key, off, nxt) in enumerate(bounds)})
    return {key: (int(highs[f"e{i}"]) - off) // BLOCK_STRIDE + 1
            for i, (key, off, _nxt) in enumerate(bounds) if highs.get(f"e{i}") is not None}


def _ingest(session, *, transcript_id, events, index_offset, live, binding,
            fan_out_held=True) -> int:
    """`fan_out_held=False`: a row the session already held before this batch is
    not sent to watchers again — they have it (the turn-end ship of rows a live
    tail already shipped, or a resumed conversation's earlier turns)."""
    from apps.canopy_sessions import services as chat_services
    from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
    from apps.realtime import groups

    already: set[int] = set()
    if not fan_out_held:
        shift = chat_transcript_ingest._index_offset(session)
        already = {i - shift for i in Message.objects.filter(
            session=session,
            turn_index__in=[e.index + shift for e in events if e.index >= 0],
        ).values_list("turn_index", flat=True)}

    # transcript ordinal (e.index) -> the author persist_transcript_rows found
    # for it, if any — carried into the live frame below so a watcher sees who
    # sent an unmarked line without waiting for a reload (stream_map's
    # kind=="user" branch reads this when its own marker-parse finds none).
    authors_by_index: dict[int, dict] = {}
    if chat_services.transcript_sourced(session):
        # BEFORE any write: if these ordinals index a different transcript than the
        # rows already held, the two would interleave (or the new one be silently
        # swallowed). If a closed session's NAME was reused, these rows are a new
        # conversation's and go to a new session, leaving the old one whole;
        # otherwise the new transcript opens a new epoch above the held rows.
        if binding is not None and chat_transcript_ingest.fork_if_name_reused(session, transcript_id):
            binding = RunnerBinding.objects.select_related("session").get(pk=binding.pk)
            session = binding.session
        chat_transcript_ingest.ensure_transcript_identity(session, transcript_id)
        # Not "was this session discovered in emdash?" — where a conversation
        # started says nothing about where its record belongs. A phone-created chat
        # is driven by the same runner, in the same emdash session, writing the same
        # transcript, so it persists the same way (see services.transcript_sourced).
        #
        # Ordinal-less events (an old runner) stay live-view-only: persisting
        # assistant rows without the user side would blank the tail fallback's
        # human half the moment any row exists.
        # The payload IS the row's content (structured fields + "text"), stored
        # verbatim — a tool_use's {id,name,input} and a tool_result's
        # {tool_use_id,is_error} are what the client pairs and renders on, so
        # flattening to text here would strip exactly the half that makes a tool
        # call legible.
        created = chat_transcript_ingest.persist_transcript_rows(session, [
            {"index": e.index, "role": e.kind,
             "text": (e.payload or {}).get("text", ""), "content": e.payload or {},
             "cwd": e.cwd, "git_branch": e.git_branch}
            for e in events if e.index >= 0
        ])
        from apps.canopy_sessions.authorship import parse as parse_marker

        # A user row's text is the prompt AS DELIVERED — marker line included —
        # and the receivers compare it with the bare Turn.prompt (Slack's
        # "carrying on elsewhere" check), so they get the words without it.
        streamed = [(e.index, e.kind,
                     parse_marker(text)[1] if e.kind == "user" else text)
                    for e in events
                    if e.index >= 0 and e.kind in ("user", "assistant")
                    for text in [str((e.payload or {}).get("text") or "")]
                    if text]
        if created and streamed:
            from apps.harness.signals import transcript_rows_streamed

            streamed_session = session
            transaction.on_commit(lambda: transcript_rows_streamed.send(
                sender=type(streamed_session), session=streamed_session, rows=streamed))
        user_indices = [
            e.index + index_offset
            for e in events if e.index >= 0 and e.kind == "user"
        ]
        if user_indices:
            authors_by_index = {
                turn_index - index_offset: author
                for turn_index, author in Message.objects.filter(
                    session=session, turn_index__in=user_indices,
                ).values_list("turn_index", "author")
                if author
            }
    if not live:
        # Persisted above, but nobody is watching, so there is nothing to push.
        # The runner now tails EVERY session it backs so the durable record stops
        # depending on someone having the chat open (see list_streams); this is the
        # gate that keeps that from also broadcasting every session in the fleet to
        # session groups no client has joined.
        return 0
    sgroup = groups.session_group(session.pk)
    n = 0
    from apps.canopy_sessions.transcript_noise import is_system_noise

    from . import sensitivity

    # The live frame carries what the row stores (spec 2026-10-22-sensitive-data-agents
    # §4): a watcher and a reload see the same withheld result.
    withhold = sensitivity.is_sensitive(session)

    for e in events:
        # The noise filter has to run HERE too, not just in
        # persist_transcript_rows. Now that user events fan out live, a filter
        # applied only on the durable path would drop a harness marker from
        # history while still pushing it to every watching client — so it would
        # appear live, then vanish on reload. Same rule, both paths.
        if e.kind.startswith(LIVE_ONLY_PREFIXES):
            # Fans out, never persists — index -1 already excludes it from the
            # durable write, and it has no transcript row to be.
            groups.publish(sgroup, {
                "type": "chat.turn_event",
                "event": {"kind": e.kind, "seq": e.seq, "payload": e.payload},
                "turn_id": None,
            })
            n += 1
            continue
        if e.kind == "user" and is_system_noise((e.payload or {}).get("text", "")):
            n += 1
            continue
        if e.index >= 0 and e.index in already:
            continue  # on screen since it first arrived
        # User events ARE fanned out. They used to be withheld on the grounds
        # that "the sender's client already echoed them optimistically" — true
        # for text typed in the web, false for text typed directly into emdash,
        # which no web client ever saw. The result was that typing in emdash and
        # watching on the phone silently dropped your own words until a reload
        # (observed 2026-07-27). The client upserts on turn_index, so a message
        # that does arrive twice collapses instead of doubling.
        event_payload = e.payload
        if withhold and e.kind in sensitivity.RESULT_KINDS:
            event_payload = {**sensitivity.strip_result(e.payload, session=session), "text": ""}
        if e.kind == "user":
            author = authors_by_index.get(e.index)
            if author:
                event_payload = {**(e.payload or {}), "author": author}
        groups.publish(sgroup, {
            "type": "chat.turn_event",
            "event": {"kind": e.kind, "seq": e.seq, "payload": event_payload},
            "turn_id": None,
        })
        n += 1
    return n
