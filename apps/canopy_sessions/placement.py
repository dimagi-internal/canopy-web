"""Which runner a chat runs on: who may place it where, the pin for a new
turn, transferring a session to another runner, and forking a session from a
message.
"""
from __future__ import annotations

import uuid

from canopy_transcript import BLOCK_STRIDE
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.harness import routing
from apps.harness import services as harness_services
from apps.harness.models import Turn

from . import activity as session_activity
from . import conversation_keys, services, transcript_ingest
from .models import Message, RunnerBinding, Session


def _placeable_runner(session: Session, runner_id):
    """`_eligible_runner`, then the conversation's runner requirements (`sensitive_data`)."""
    from apps.harness import runner_requirements as rr

    runner = _eligible_runner(session, runner_id)
    if runner is None:
        return None
    # A box the conversation's host does not allow is no placement at all: a pin
    # to it would sit unclaimable forever (claim_next_turn refuses it above pins).
    if not rr.runner_satisfies(runner, rr.requirements_of_session(session)):
        return None
    # A chat WITH an agent runs as that agent on the box it lands on, so the box
    # must be one that may hold the agent (agents.services.runner_may_hold_agent:
    # its owner is the agent's admin, or the agent's own login) — the rule
    # claiming, credential resolve and every routing write already apply. A
    # transfer approved by a box's admin could otherwise move an agent's
    # conversation onto a box nobody trusted with the agent.
    if session.agent_id:
        from apps.agents.services import runner_may_hold_agent

        if not runner_may_hold_agent(runner, session.agent):
            return None
    return runner


def _placement_refused(session: Session, runner_id, default: str) -> ValueError:
    """The error for a placement `_placeable_runner` refused. Names the
    requirement when the runner is one the caller could otherwise place on —
    "unknown runner" would be false for a box their own fleet lists — and
    `default` otherwise (an invisible id stays indistinguishable from a
    nonexistent one)."""
    from apps.harness import runner_requirements as rr

    if _eligible_runner(session, runner_id) is not None:
        reqs = rr.requirements_of_session(session)
        return ValueError(f"this conversation requires a {rr.describe(reqs)} runner")
    return ValueError(default)


def _eligible_runner(session: Session, runner_id):
    """A runner may be a placement target only if it could actually CLAIM this
    session's turns — its owner belongs to the session's workspace (mirrors
    claim_next_turn's tenant derivation from owner; a foreign or orphaned
    runner would leave the pinned turn permanently unclaimable) AND it is
    session-capable (capabilities.sessions — the runner-side truth for who
    may execute a chat turn; a pin can't override that). Invisible ids
    resolve to None so callers 422 exactly like a nonexistent id (no oracle);
    a malformed id (not a UUID) resolves to None the same way rather than
    raising django's ValidationError out of the ORM lookup."""
    from apps.harness.models import Runner
    from apps.workspaces import services as wsvc

    if not runner_id:
        return None
    try:
        uuid.UUID(str(runner_id))
    except (ValueError, AttributeError, TypeError):
        return None
    runner = (
        Runner.objects.filter(id=runner_id, owner__isnull=False)
        .exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
        .first()
    )
    if runner is None:
        return None
    if not runner.session_capable():
        return None
    if not wsvc.is_member(runner.owner, session.workspace_id):
        return None
    return runner


#: Why choosing a box for a project chat was refused (`may_place`).
PROJECT_CHAT_PLACEMENT_REFUSED = (
    "starting a project chat on {runner} is for someone who administers that runner; "
    "start it without choosing a runner and the workspace's runner order, or a box "
    "you run, takes it")

#: Why choosing a box for a chat with an agent was refused (`may_place`).
AGENT_CHAT_PLACEMENT_REFUSED = (
    "running {agent} on {runner} is for the agent's owner or admins, or for someone "
    "who administers that runner")


def may_place(user, session: Session, runner) -> bool:
    """May `user` choose `runner` for this chat's turns? The ONE rule every door
    that chooses a box asks — a send with `placement`, re-pinning a queued turn
    (`place_queued_turn`), moving queued turns (`move_queued_turns`, and Slack's
    move-to-cloud through it) and the cloud offer (`available_cloud_runner`).

    * A chat WITH an agent: `agents.access.may_pin_runner` — its admins may pin
      any box that can hold it, anyone else only a box they administer.
    * A PROJECT chat (no agent): they administer the box
      (`harness.runners.can_administer_runner`) — the box runs their prompt in
      its owner's repo checkout, which only the workspace's runner order or the
      box's own administrators may decide.

    That the box can take the chat at all (`_placeable_runner`) is checked
    separately, and first. Nobody (`user` None or anonymous) may choose a box."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if session.agent_id:
        from apps.agents.access import may_pin_runner

        return may_pin_runner(user, session.agent, runner)
    from apps.harness.runners import can_administer_runner

    return can_administer_runner(user, runner)


def _placement_forbidden(session: Session, runner) -> ValueError:
    if session.agent_id:
        return ValueError(AGENT_CHAT_PLACEMENT_REFUSED.format(
            agent=session.agent.slug, runner=runner.name))
    return ValueError(PROJECT_CHAT_PLACEMENT_REFUSED.format(runner=runner.name))


def _resolve_placement(session: Session, placement: str | None, user=None):
    """Directed-placement pin for a NEW turn about to be enqueued. `placement`
    wins when given explicitly; otherwise an unbound session's stashed
    `requested_runner_id` (set at directed-new-chat creation) pins the first
    turn. A live binding needs no pin here — claim-time stickiness already
    routes the turn to the binding holder (see claim_next_turn's session leg).

    Returns a Runner|None. Raises ValueError for an explicit but unresolvable
    placement (unknown/retired/foreign-tenant runner) — the caller surfaces
    that as a 422."""
    if placement == "wait":
        binding = getattr(session, "runner_binding", None)
        return binding.runner if binding and binding.runner_id else None
    if placement:
        pinned = _placeable_runner(session, placement)
        if pinned is None:
            raise _placement_refused(session, placement, "unknown runner for placement")
        if not may_place(user, session, pinned):
            raise _placement_forbidden(session, pinned)
        return pinned
    if not getattr(session, "runner_binding", None):
        rid = (session.metadata or {}).get("requested_runner_id")
        if rid:
            return _placeable_runner(session, rid)
    return None



# Prepended to every transfer brief, server-side, so no caller can forget it.
#
# The receiving session is NOT a resumed conversation. A transfer re-points the
# binding at a box that cannot reach the old box's claude session, so the new
# runner opens a fresh one: it arrives with none of the thread above it in
# context, while the web UI shows it the whole history — which reads, to it, as
# if it should already know all of this. Saying so plainly is what stops it
# confidently re-deriving (or redoing) work that already shipped. Proven by hand
# on 2026-09-12: a transferred session given this preamble plus a git-state brief
# verified two PRs and a file checksum and then correctly stopped, rather than
# starting over.
TRANSFER_PREAMBLE = """\
**This session has been transferred from {source} to {target}.**

You are a FRESH session picking up this thread. The conversation above happened \
on another box, in a different checkout — you do NOT have it in context, even \
though the chat history is showing it to you. Everything you can rely on is \
below. Read it, verify the state yourself, then report and wait; do not start \
new work or redo anything already described as shipped.

Anything that was never pushed from {source} did not come with you.
"""


def _initiator(initiator, user, via: str):
    """The turn's asker (`apps/harness/initiator.py`). Callers pass it, because
    only the caller knows how the person authenticated. A caller that has not
    been taught to still gets the right PERSON with an empty assurance — true
    about who, silent about how — rather than a guessed grade."""
    if initiator is not None:
        return initiator
    from apps.harness import initiator as who
    return who.for_user(user, via=via, assurance="")


def transfer_session(*, session: Session, placement: str, brief: str = "", user=None,
                     initiator=None):
    """Move a live session onto another runner, carrying its history.

    The three things a transfer has to do, which pinning a turn alone does NOT:

    1. **Re-point the binding.** A pinned turn lands on the target box, but the
       binding still names the old one, so `post_session_stream`'s
       runner-owned-binding gate (404s a non-owner) drops everything the new box
       ships until its first `record_session` happens to fix it up. Moving it here
       makes the transfer the thing that decides placement, rather than a race.
    2. **Open a new epoch.** The target cannot resume the source's claude session,
       so it starts a fresh transcript whose ordinals restart near 0. Those would
       collide with the rows already held and, worse, trip
       `ensure_transcript_identity` into deleting them. `index_offset` lifts the
       new box's ordinals above the inherited history instead. See its docstring
       on the model — this is the field's entire reason to exist.
    3. **Hand over context.** The new session is cold. `TRANSFER_PREAMBLE` plus the
       caller's `brief` become the prompt of a turn pinned to the target, which is
       the only thing standing between "picked up the thread" and "started again
       from a blank worktree".

    `placement` is a runner UUID (resolved through the same `_placeable_runner`
    gate `send_message` uses, so a foreign or non-session-capable box is refused
    rather than left pinned-and-unclaimable forever).

    Raises ValueError for an unresolvable/ineligible target, LookupError for a
    session with no binding to move, and RuntimeError while a turn is still
    executing — a box mid-thought would keep writing into the epoch we are about
    to close, so the caller stops the session first (`POST /{id}/stop`) and
    retries. Returns (binding, turn).
    """
    target = _placeable_runner(session, placement)
    if target is None:
        raise _placement_refused(session, placement, "unknown runner for transfer")
    if session.status != Session.ACTIVE:
        raise ValueError("cannot transfer an archived session")
    if Turn.objects.filter(
        chat_session=session, status__in=list(routing.EXECUTING)
    ).exists():
        raise RuntimeError("a turn is still executing — stop the session first")

    with transaction.atomic():
        binding = (
            RunnerBinding.objects.select_for_update().filter(session=session).first()
        )
        if binding is None:
            raise LookupError("session has no runner binding to transfer")
        source = binding.runner
        if source is not None and source.id == target.id:
            raise ValueError(f"session is already on '{target.name}'")

        transcript_ingest._open_epoch(binding, session)

        binding.runner = target
        binding.transferred_from = source
        binding.transferred_at = timezone.now()
        # Everything below describes the SOURCE box's screen, and none of it is
        # true of the target. `session_key` and `host` in particular are what
        # `reusable_by` consults: left populated they would tell the target to go
        # drive a task that does not exist on it. Cleared, `resolve_session`
        # returns reuse=False + new_thread=False, which is precisely "open a fresh
        # session under this account and rehydrate from `summary`" — the path the
        # two-account failover already uses.
        binding.session_key = ""
        binding.host = ""
        binding.transcript_id = ""
        binding.pending_question = None
        binding.pending_answer = None
        binding.close_requested = False
        binding.agent_status = ""
        binding.agent_status_stale = False
        binding.tail = []
        binding.save(update_fields=[
            "runner", "transferred_from", "transferred_at", "index_offset",
            "session_key", "host", "transcript_id", "pending_question",
            "pending_answer", "close_requested", "agent_status",
            "agent_status_stale", "tail", "updated_at",
        ])

        # So a LATER send on a still-unbound session re-pins here too, instead of
        # falling back to open routing and landing on whichever box polls first.
        services._merge_metadata(session, {"requested_runner_id": str(target.id)})

        source_name = source.name if source is not None else "an unknown runner"
        prompt = TRANSFER_PREAMBLE.format(source=source_name, target=target.name)
        if brief.strip():
            prompt = f"{prompt}\n{brief.strip()}\n"
        thread_key = binding.thread_key or str(session.id)
        turn, _created = harness_services.enqueue_turn(
            session=session,
            origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
            # Keyed on the TARGET and the transfer's own timestamp: transferring
            # back and forth (which is the normal shape of a failover) must not
            # dedupe onto the outbound turn.
            idempotency_key=(
                f"transfer:{session.id.hex}:{target.id.hex}:"
                f"{int(binding.transferred_at.timestamp())}"
            ),
            prompt=prompt,
            origin_ref={"thread_key": thread_key, "chat_session_id": str(session.id),
                        "transfer_from": source_name},
            enqueued_by=user,
            pinned_runner=target,
            initiator=_initiator(initiator, user, "transfer"),
            # The session being moved is what this handoff turn comes from.
            parent={"session": session},
            provenance_extra={"transfer_from": source_name},
        )
    return binding, turn


#: Where a fork records what its first turn must copy (`resolve_session` hands it
#: to the runner): the source's Claude session, how many of its transcript records
#: to keep, the canopy session it came from, and `worktree_of` — the session whose
#: directory it runs in on a cloud runner (the root of a chain of forks).
FORK_KEY = "fork"


def fork_runner_supported(runner) -> bool:
    """Whether `runner` can cut a fork from a session's transcript: one that runs
    Claude itself, so it can write a trimmed copy of the transcript and resume it —
    a cloud runner, or a laptop on the `claude-cli` runtime. emdash and the Claude
    app own their sessions."""
    from apps.harness.models import Runner

    return runner is not None and (runner.kind == Runner.CLOUD or runner.engine == Runner.ENGINE_CLAUDE_CLI)


def fork_session(*, session: Session, turn_index: int, user) -> Session:
    """A new session holding `session`'s conversation up to the end of the turn
    that `turn_index` belongs to, running in the SAME worktree on the same runner.

    The cut is always a turn boundary — just before the next prompt a person sent —
    so the fork never holds a tool call without its result. The history is copied
    now so it shows at once; the transcript is cut by the runner on the fork's
    first turn (`resolve_session` returns `fork`), and its first ship replaces these
    rows with ones derived from that copy (`ensure_transcript_identity`).

    Files are shared, not forked: both sessions edit the one worktree.

    Raises LookupError when there is no such message, ValueError when the session
    cannot be forked (no Claude session, a runner that cannot, a message from
    before a transfer), RuntimeError when the cut would fall in a turn still running.
    """
    binding = getattr(session, "runner_binding", None)
    if binding is None or not binding.session_key or binding.runner is None:
        raise ValueError("this session has no Claude session on a runner to fork")
    if not services.transcript_sourced(session):
        raise ValueError("this session's history is not a Claude transcript, so it cannot be forked")
    if not fork_runner_supported(binding.runner):
        raise ValueError(f"forking needs a cloud runner or one on the Claude CLI runtime; "
                         f"{binding.runner.name} runs {binding.runner.engine}")
    offset = binding.index_offset
    clicked = Message.objects.filter(session=session, turn_index=turn_index).first()
    if clicked is None:
        raise LookupError("no such message in this session")
    if turn_index < offset:
        raise ValueError("that message is from before this session moved runners; "
                         "its transcript is not on this runner")
    next_prompt = (Message.objects.filter(session=session, role=Message.USER, turn_index__gt=turn_index)
                   .order_by("turn_index").first())
    if next_prompt is not None:
        records = (next_prompt.turn_index - offset) // BLOCK_STRIDE
    else:
        if Turn.objects.filter(chat_session=session, status__in=list(Turn.NON_TERMINAL)).exists():
            raise RuntimeError("the last turn is still running — fork from an earlier message, "
                               "or wait for it to finish")
        high = Message.objects.filter(session=session).aggregate(m=Max("turn_index"))["m"]
        records = (int(high) - offset) // BLOCK_STRIDE + 1

    from apps.harness import runner_requirements as rr

    title = (session.title or "").strip() or "session"
    with transaction.atomic():
        fork = services.create_session(
            workspace=session.workspace, created_by=user, agent=session.agent,
            project=session.project, title=f"Fork of {title}"[:200],
            metadata={
                FORK_KEY: {"session_key": binding.session_key, "records": records,
                           "from_session": str(session.id),
                           "worktree_of": worktree_of(session)},
                # The fork's first turn must reach the box holding the transcript.
                "requested_runner_id": str(binding.runner_id),
                # Its history is a cut of a transcript, whatever the executor.
                services.TRANSCRIPT_SOURCED: True,
                # The fork holds the same conversation, so the same runners: a fork
                # never sheds a requirement (spec 2026-10-22-sensitive-data-agents).
                **({rr.METADATA_KEY: (session.metadata or {})[rr.METADATA_KEY]}
                   if (session.metadata or {}).get(rr.METADATA_KEY) else {}),
            },
        )
        fork.parent_session = session
        fork.parent_claude_session = binding.session_key
        fork.ordinal_scheme = session.ordinal_scheme
        fork.save(update_fields=["parent_session", "parent_claude_session", "ordinal_scheme"])
        Message.objects.bulk_create([
            Message(session=fork, turn_index=m.turn_index - offset, role=m.role, content=m.content,
                    plaintext=m.plaintext, author=m.author, source_turn_id=m.source_turn_id,
                    sealed=m.sealed)
            for m in Message.objects.filter(
                session=session, turn_index__gte=offset,
                turn_index__lt=offset + records * BLOCK_STRIDE)
        ])
        # The same conversation, so the same key and the same people's wraps: the
        # copied rows are sealed to it (`conversation_keys.copy_key`).
        conversation_keys.copy_key(session, fork)
        session_activity.rebuild(Session.objects.select_for_update().get(pk=fork.pk))
    return fork


def worktree_of(session) -> str:
    """The canopy session whose directory `session` runs in on a cloud runner: its
    own, or for a fork the one its source runs in."""
    return ((session.metadata or {}).get(FORK_KEY) or {}).get("worktree_of") or str(session.id)


def fork_plan(thread_key: str) -> tuple[dict | None, str]:
    """For a forked chat: the cut its first turn must make (None once the runner has
    recorded the fork's own Claude session — later turns resume it like any other),
    and the session whose directory it runs in. (None, "") for anything else."""
    try:
        sid = uuid.UUID(thread_key)
    except (TypeError, ValueError):
        return None, ""
    session = Session.objects.filter(pk=sid).select_related("runner_binding").first()
    fork = ((session.metadata or {}).get(FORK_KEY) if session is not None else None)
    if not fork:
        return None, ""
    binding = getattr(session, "runner_binding", None)
    pending = None if binding is not None and binding.session_key else {
        "session_key": fork["session_key"], "records": int(fork["records"])}
    return pending, worktree_of(session)
