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

from apps.harness import clis, routing, sensitivity
from apps.harness.models import Runner, Turn

from . import activity as session_activity
from . import conversation_keys, handoff, services, staleness, transcript_read
from . import lifecycle
from .models import Message, RunnerBinding, Session


def _placeable_runner(session: Session, runner_id, *, cli: str = ""):
    """`_eligible_runner`, then the conversation's runner requirements (`sensitive_data`)
    and its agent CLI — `cli` when a transfer moves it to another one."""
    from apps.harness import runner_requirements as rr

    runner = _eligible_runner(session, runner_id)
    if runner is None:
        return None
    # A box the conversation's host does not allow is no placement at all: a pin
    # to it would sit unclaimable forever (claim_next_turn refuses it above pins).
    if not rr.runner_satisfies(runner, rr.requirements_of_session(session)):
        return None
    # Likewise a box without the agent CLI the conversation is held to
    # (`harness.clis`): no claim on it could ever run the conversation's turns.
    if (cli or session.cli) and (cli or session.cli) not in clis.reported(runner):
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


def _placement_refused(session: Session, runner_id, default: str, *, cli: str = "") -> ValueError:
    """The error for a placement `_placeable_runner` refused. Names the
    requirement when the runner is one the caller could otherwise place on —
    "unknown runner" would be false for a box their own fleet lists — and
    `default` otherwise (an invisible id stays indistinguishable from a
    nonexistent one)."""
    from apps.harness import runner_requirements as rr

    runner = _eligible_runner(session, runner_id)
    if runner is not None:
        held = cli or session.cli
        if held and held not in clis.reported(runner):
            other = [c for c in clis.offered(session.agent) if c in clis.reported(runner)]
            hint = (f" — transfer it with cli={other[0]} to continue on {clis.label(other[0])} "
                    "from a fresh session" if other and not cli else "")
            return ValueError(f"this conversation {'would run' if cli else 'runs'} on "
                              f"{clis.label(held)}, and {runner.name} does not have it{hint}")
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


def busy_reason(session: Session) -> str:
    """Why `session` cannot be moved right now, or "" when it can.

    Two ways a session is mid-thought: a turn that drives it executing
    (`staleness.driving_turns_of` — a chat turn, or an agent turn a cloud runner
    keeps RUNNING for the whole of the work), or the engine itself working — a
    person typing straight into the session runs Claude with no executing `Turn`
    at all. The engine's word counts whenever its runner is reachable, PAUSED
    included; an offline box's last report describes a session nobody can still
    be driving, so moving off it stays allowed."""
    if staleness.driving_turns_of(session).filter(status__in=list(routing.EXECUTING)).exists():
        return "a turn is still executing — stop the session first"
    binding = getattr(session, "runner_binding", None)
    if (binding is not None and binding.runner_id is not None and binding.runner.is_reachable
            and transcript_read.engine_says_running(binding)):
        return "the session is working right now — stop it first"
    return ""


def cli_switch_refusal(session: Session, cli: str) -> str | None:
    """Why `session` may not continue on agent CLI `cli`, or None. A switch is a
    fresh start in that CLI from the handoff's prompt (never a resume), so it is
    allowed to any CLI its agent is set up for — except one that may not hold a
    sensitive conversation."""
    if not cli or cli == session.cli:
        return None
    if cli not in clis.offered(session.agent):
        who = session.agent.name if session.agent else "a chat without an agent"
        return f"{who} isn't set up to run on {clis.label(cli)}"
    if sensitivity.is_sensitive(session) and cli not in clis.SENSITIVE_CLIS:
        return f"a conversation holding sensitive data cannot move to {clis.label(cli)}"
    return None


def transfer_session(*, session: Session, placement: str, brief: str = "", user=None,
                     initiator=None, cli: str = ""):
    """Move a live session onto another runner, carrying its history AND its work.

    What a transfer has to do, which pinning a turn alone does NOT:

    1. **Re-point the binding.** A pinned turn lands on the target box, but the
       binding still names the old one, so `post_session_stream`'s
       runner-owned-binding gate (404s a non-owner) drops everything the new box
       ships until its first `record_session` happens to fix it up. Moving it here
       makes the transfer the thing that decides placement, rather than a race.
    2. **Carry the work** (`handoff.py`). The source box is asked to pack its
       worktree — branch, local-only commits, diffs, untracked files — and the
       target restores it before its agent acts. The turn waits for that: it is
       enqueued when the source answers (or `handoff.CAPTURE_WAIT` passes), not
       here. A source that cannot answer transfers at once with NO SNAPSHOT, and
       the prompt says so.
    3. **Open a new epoch** — at settle, unless the target resumes the source's
       own Claude session (a cloud box given the transcript). A fresh Claude
       session restarts its ordinals near 0; `index_offset` lifts them above the
       inherited history instead of on top of it (see the field's docstring).
    4. **Hand over context.** `TRANSFER_PREAMBLE`, what the snapshot holds, and
       the caller's `brief` become the prompt of a turn pinned to the target.

    `cli` continues the conversation on another agent CLI (`cli_switch_refusal`):
    the target must have it, and starts it fresh — nothing resumes across CLIs.

    `placement` is a runner UUID (resolved through the same `_placeable_runner`
    gate `send_message` uses, so a foreign or non-session-capable box is refused
    rather than left pinned-and-unclaimable forever).

    Raises ValueError for an unresolvable/ineligible target, LookupError for a
    session with no binding to move, and RuntimeError while a turn is still
    executing — a box mid-thought would keep writing into the epoch we are about
    to close, so the caller stops the session first (`POST /{id}/stop`) and
    retries. Returns (binding, turn); `turn` is None while the source packs
    (`session.handoffs.first()` says how that is going).
    """
    to_cli = cli if cli and cli != session.cli else ""
    if (refused := cli_switch_refusal(session, to_cli)) is not None:
        raise ValueError(refused)
    target = _placeable_runner(session, placement, cli=to_cli)
    if target is None:
        raise _placement_refused(session, placement, "unknown runner for transfer", cli=to_cli)
    if session.status != Session.ACTIVE:
        raise ValueError("cannot transfer an archived session")
    if reason := busy_reason(session):
        raise RuntimeError(reason)

    with transaction.atomic():
        binding = (
            RunnerBinding.objects.select_for_update().filter(session=session).first()
        )
        if binding is None:
            raise LookupError("session has no runner binding to transfer")
        source = binding.runner
        if source is not None and source.id == target.id:
            raise ValueError(f"session is already on '{target.name}'")

        # Before the source's identity is cleared below: the handoff is now the
        # only record of which task on which box holds the work.
        h = handoff.begin(session=session, binding=binding, source=source, target=target,
                          brief=brief, user=user,
                          initiator=_initiator(initiator, user, "transfer"), to_cli=to_cli)

        binding.runner = target
        binding.transferred_from = source
        binding.transferred_at = timezone.now()
        # Everything below describes the SOURCE box's screen, and none of it is
        # true of the target. `session_key` and `host` in particular are what
        # `reusable_by` consults: left populated they would tell the target to go
        # drive a task that does not exist on it. Cleared, `resolve_session`
        # returns reuse=False + new_thread=False, which is precisely "open a fresh
        # session under this account and rehydrate from `summary`" — the path the
        # two-account failover already uses. (`transcript_id` and the epoch are
        # decided at settle: they depend on whether the target resumes.)
        binding.session_key = ""
        binding.host = ""
        binding.pending_question = None
        binding.pending_answer = None
        binding.close_requested = False
        binding.agent_status = ""
        binding.agent_status_stale = False
        binding.tail = []
        binding.save(update_fields=[
            "runner", "transferred_from", "transferred_at",
            "session_key", "host", "pending_question",
            "pending_answer", "close_requested", "agent_status",
            "agent_status_stale", "tail", "updated_at",
        ])

        # So a LATER send on a still-unbound session re-pins here too, instead of
        # falling back to open routing and landing on whichever box polls first.
        services._merge_metadata(session, {"requested_runner_id": str(target.id)})

        if h.capture_status != handoff.SessionHandoff.PENDING:
            h = handoff.settle(h)
        lifecycle.record(lifecycle.TRANSFERRED, session, by=user,
                         change={"field": "runner", "before": str(source.id) if source else None,
                                 "after": str(target.id)},
                         handoff=str(h.pk), to_cli=to_cli)
    binding.refresh_from_db()
    return binding, h.turn


def place_queued_turn(*, session: Session, placement: str, user=None) -> Turn:
    """Re-pin a session's oldest QUEUED turn — the chat banner's after-the-fact
    directed-placement decision (vs `_resolve_placement`, which only applies to
    a turn being newly enqueued). `placement` is "wait" (pin to the session's
    currently bound runner) or a runner UUID string.

    Raises LookupError if there is no queued turn to place (-> 404), or
    ValueError for an unresolvable placement (-> 422): "wait" with no bound
    runner, an unknown/retired runner id, or a box `user` may not choose
    (`may_place`).
    """
    turn = (
        Turn.objects.filter(chat_session=session, status=Turn.QUEUED)
        .order_by("created_at")
        .first()
    )
    if turn is None:
        raise LookupError("no queued turn to place")
    if placement == "wait":
        binding = getattr(session, "runner_binding", None)
        if not (binding and binding.runner_id):
            raise ValueError("session has no bound runner to wait for")
        turn.pinned_runner_id = binding.runner_id
    else:
        runner = _placeable_runner(session, placement)
        if runner is None:
            raise _placement_refused(session, placement, "unknown runner")
        if not may_place(user, session, runner):
            raise _placement_forbidden(session, runner)
        turn.pinned_runner = runner
    turn.save(update_fields=["pinned_runner"])
    return turn


def available_cloud_runner(session: Session, user=None):
    """An ONLINE, session-capable cloud runner this session could be placed on,
    or None. Same `_placeable_runner` gate as every other placement, so the
    answer is never a box that would leave the turn pinned and unclaimable — and
    one `user` (who sent it) may place it on (`may_place`), so the offer is never
    one the click refuses. A
    directed-only box (a test box) is never offered unasked: it takes work only
    when someone chooses it (the pickers still list it)."""
    for runner in (Runner.objects.filter(kind=Runner.CLOUD, owner__isnull=False)
                   .exclude(status=Runner.RETIRED).order_by("name")):
        if runner.live_status != Runner.ONLINE or runner.directed_only():
            continue
        if (_placeable_runner(session, str(runner.id)) is not None
                and may_place(user, session, runner)):
            return runner
    return None


MOVE_BRIEF = (
    "{source} was offline with {n} message(s) waiting in this conversation, so it was "
    "moved here. Those messages arrive as the next turn(s) — answer them once you have "
    "checked the state above."
)


def move_queued_turns(*, session: Session, placement: str, user=None, initiator=None) -> list[Turn]:
    """Send a session's QUEUED turns to another runner — the "its box is offline,
    run it somewhere else" move. Returns the turns moved.

    Two cases, because a session that already lives on a box cannot simply have
    its next turn pinned elsewhere (that is `place_queued_turn`, and the
    `transfer_session` docstring records what it cost when done by hand):

    * **Unbound** (a new conversation whose first message never got picked up):
      nothing exists on any box yet, so pinning the waiting turns is the whole
      move, and `requested_runner_id` makes later sends follow.
    * **Bound** to another box: a real transfer, carrying the history and a
      handoff turn. The handoff runs BEFORE the waiting messages — the new box
      is cold, and answering them first is answering blind — because the claim
      holds a session's other turns until its transfer turn has started
      (`routing.transfer_held_q`).

    Raises like `transfer_session`: ValueError (bad target), LookupError
    (nothing queued), RuntimeError (a turn is still executing).
    """
    target = _placeable_runner(session, placement)
    if target is None:
        raise _placement_refused(session, placement, "unknown runner")
    if not may_place(user, session, target):
        raise _placement_forbidden(session, target)
    queued = list(Turn.objects.filter(chat_session=session, status=Turn.QUEUED).order_by("created_at"))
    if not queued:
        raise LookupError("no queued turn to move")
    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    if binding is not None and binding.runner_id and binding.runner_id != target.id:
        transfer_session(
            session=session, placement=str(target.id),
            brief=MOVE_BRIEF.format(source=binding.runner.name, n=len(queued)),
            user=user, initiator=initiator,
        )
    else:
        services._merge_metadata(session, {"requested_runner_id": str(target.id)})
    Turn.objects.filter(pk__in=[t.pk for t in queued], status=Turn.QUEUED).update(pinned_runner=target)
    for t in queued:
        t.refresh_from_db()
    return [t for t in queued if t.pinned_runner_id == target.id]


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
                           "worktree_of": worktree_of(session),
                           # Copied rows move down by `offset`; their sealed envelopes keep
                           # the index they were sealed at (`conversation_keys.index_offsets`).
                           **_index_offsets(session, offset)},
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
        # The cut is of the source's own store, so the fork runs on its CLI.
        fork.cli = session.cli
        fork.save(update_fields=["parent_session", "parent_claude_session", "ordinal_scheme", "cli"])
        Message.objects.bulk_create([
            Message(session=fork, turn_index=m.turn_index - offset, role=m.role, content=m.content,
                    plaintext=m.plaintext, author=m.author, source_turn_id=m.source_turn_id,
                    sealed=m.sealed, typed=m.typed)
            for m in Message.objects.filter(
                session=session, turn_index__gte=offset,
                turn_index__lt=offset + records * BLOCK_STRIDE)
        ])
        # The same conversation, so the same key and the same people's wraps: the
        # copied rows are sealed to it (`conversation_keys.copy_key`).
        conversation_keys.copy_key(session, fork)
        session_activity.rebuild(Session.objects.select_for_update().get(pk=fork.pk))
        lifecycle.record(lifecycle.FORKED, session, by=user, fork=str(fork.pk))
    return fork


def _index_offsets(source, offset: int) -> dict:
    earlier = conversation_keys.index_offsets(source)
    if earlier is None:
        return {}
    return {"index_offsets": sorted({int(offset), *(int(offset) + d for d in earlier)})}


#: How far up a chain of forks `fork_lineage` walks.
MAX_FORK_DEPTH = 32


def fork_lineage(session) -> list[str]:
    """`session` and the sessions it was forked from, nearest first, read from the
    `parent_session` column canopy itself sets (`fork_session`) — never from metadata."""
    out, current = [str(session.id)], session
    while current.parent_session_id and len(out) < MAX_FORK_DEPTH:
        if str(current.parent_session_id) in out:
            break
        out.append(str(current.parent_session_id))
        current = Session.objects.filter(pk=current.parent_session_id).only(
            "id", "parent_session_id").first()
        if current is None:
            break
    return out


def worktree_of(session) -> str:
    """The canopy session whose directory `session` runs in on a cloud runner: its
    own, or for a fork the one its source runs in — and only if that one really is in
    its lineage (`fork_lineage`), so no metadata can aim a turn at someone else's
    directory, where their transcripts sit."""
    named = ((session.metadata or {}).get(FORK_KEY) or {}).get("worktree_of") or ""
    if named and named != str(session.id) and named in fork_lineage(session):
        return named
    return str(session.id)


def fork_plan(thread_key: str) -> tuple[dict | None, str, list[str]]:
    """For a forked chat: the cut its first turn must make (None once the runner has
    recorded the fork's own Claude session — later turns resume it like any other),
    the session whose directory it runs in, and its lineage (`fork_lineage`, which the
    runner checks that directory against). (None, "", []) for anything else.

    The cut is the Claude session canopy recorded as the fork's parent
    (`parent_claude_session`), never one named only in metadata."""
    try:
        sid = uuid.UUID(thread_key)
    except (TypeError, ValueError):
        return None, "", []
    session = Session.objects.filter(pk=sid).select_related("runner_binding").first()
    fork = ((session.metadata or {}).get(FORK_KEY) if session is not None else None)
    if not isinstance(fork, dict) or not session.parent_session_id:
        return None, "", []
    lineage = fork_lineage(session)
    binding = getattr(session, "runner_binding", None)
    pending = None
    if not (binding is not None and binding.session_key):
        if session.parent_claude_session and fork.get("session_key") == session.parent_claude_session:
            try:
                pending = {"session_key": session.parent_claude_session,
                           "records": int(fork.get("records") or 0)}
            except (TypeError, ValueError):
                pending = None
    return pending, worktree_of(session), lineage
