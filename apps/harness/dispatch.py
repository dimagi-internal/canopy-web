"""The decision->work edge: an approved ask becomes Turns.

`dispatch[]` was never a new concept — it is a deferred Turn enqueue. Ada's
`{target_agent, prompt, origin, origin_ref}`, the phone composer's
`{agent_slug, prompt, origin}`, and Turn are three spellings of one payload.
TurnSpec is that payload, named once.

See docs/superpowers/specs/2026-07-15-item-and-turn-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.agents.models import Agent

from . import services
from .dispatch_marker import stamp_dispatched, wrap_human_reply
from .models import Turn


@dataclass(frozen=True)
class TurnSpec:
    """One deferred Turn enqueue.

    `target_agent=""` means SELF — the ask's own agent. Self-dispatch is the
    default and needs no ceremony; Ada's cross-agent fan-out is this same field
    set to another slug. A parameter, not a code path.
    """

    prompt: str
    target_agent: str = ""
    origin: str = Turn.ORIGIN_API
    origin_ref: dict = field(default_factory=dict)
    routing: str = Turn.PREFER_LOCAL

    @classmethod
    def from_dict(cls, d: dict) -> TurnSpec:
        return cls(
            prompt=(d.get("prompt") or "").strip(),
            target_agent=(d.get("target_agent") or "").strip(),
            origin=(d.get("origin") or Turn.ORIGIN_API).strip(),
            origin_ref=d.get("origin_ref") or {},
            routing=(d.get("routing") or Turn.PREFER_LOCAL).strip(),
        )


def _with_reply(prompt: str, reply: str, answered_by: str = "", *, label: str = "ANSWERED BY") -> str:
    """Carry the human's own words to the agent that will act on them.

    A card's `prompt` is written BEFORE the human replies, so on its own it is the
    proposal, not the decision. The reply is where the steer lives — "yes, but send
    it to hal instead", "only the retry, skip the backoff" — and an agent that never
    sees it executes a brief the human already amended.
    """
    reply = (reply or "").strip()
    if not reply:
        return prompt
    # The reply is DELIMITED, not just concatenated. `agent_review` drops a user turn carrying
    # the dispatch marker WHOLE, so gluing the human's words onto a stamped brief threw them
    # away with it — and the reply is the highest-value human signal on the board: it is the
    # human overruling, narrowing, or redirecting an agent's proposal, which is precisely what
    # a corrections lens exists to find. Delimiters let the stripper keep the human's words and
    # discard everything around them, including the boilerplate below (which says OVERRIDES and
    # "instead of", and scores as a forceful correction entirely on its own).
    return (
        f"{prompt}\n\n---\n"
        f"{label} {answered_by or 'a human'}: {wrap_human_reply(reply)}\n\n"
        f"That reply is the authority on this card and OVERRIDES the brief above wherever "
        f"the two disagree. If it redirects the work, narrows it, declines it, or asks a "
        f"question back, do THAT — and report on the task instead of executing the "
        f"original proposal."
    )


def _dispatch_initiator(task, action):
    """Who this dispatched work is for. The person who APPROVED the task, when a
    person did — they are the one who authorised it, whatever agent drafted the
    card. With no human behind the action, it is the agent that raised it."""
    from . import initiator as who
    via = f"task:{task.agent.slug}/{task.ext_id}"
    if action.by_user_id or getattr(action, "by_user", None) is not None:
        return who.for_user(action.by_user, via=via, assurance=who.APPROVAL)
    return who.for_agent(task.agent.slug, via=via)


def dispatch(task, *, action, actor_workspace_ids: set[str]) -> list[Turn]:
    """Enqueue an approved task's work: one Turn per `task.on_approve` entry.
    Idempotent per (task, index). `action` is the AgentTaskAction that approved
    it (or dispatched it); it need not be saved.

    `actor_workspace_ids` is the acting human's workspace memberships (Workspace
    pks, which are slugs). A cross-agent dispatch (`target_agent` set) is
    authorized ONLY if the target's workspace is one of them — the hard tenant
    boundary. This preserves the fleet manager (Ada dispatching hal->eva across
    workspaces works when the human driving it is a member of both), while
    blocking a single-workspace user from landing a prompt on another tenant's
    agent.

    Raises ValueError for an unknown OR cross-tenant target_agent rather than
    skipping it: an approved task whose work silently never happens is the worst
    outcome here. The caller runs this inside the same transaction as the action,
    so a raise rolls the action back and leaves the ask open and retryable.
    """
    turns: list[Turn] = []
    for i, raw in enumerate(task.on_approve or []):
        spec = TurnSpec.from_dict(raw)
        if spec.target_agent:
            target = Agent.objects.filter(slug=spec.target_agent).first()
            if target is None:
                raise ValueError(
                    f"task {task.ext_id} on_approve[{i}]: unknown target_agent {spec.target_agent!r}"
                )
            # Cross-agent dispatch is a cross-tenant action unless the actor is a
            # member of the target's workspace. Self-dispatch (below) is already
            # authorized — the actor could act on the task, which required its
            # agent's workspace. Legacy null-workspace targets fall through.
            if target.workspace_id is not None and target.workspace_id not in actor_workspace_ids:
                raise ValueError(
                    f"task {task.ext_id} on_approve[{i}]: not a member of target_agent "
                    f"{spec.target_agent!r}'s workspace"
                )
        else:
            target = task.agent
        # STAMP HERE, and only here. This is the one enqueue path where the server KNOWS the
        # prompt was written by an agent — it came off the agent's own card. `enqueue_turn`
        # itself must NOT stamp: its other callers include `canopy_sessions`, where the prompt
        # is a human typing in the web chat. Blanket-stamping would mark the human's own
        # messages as machine-dispatched and suppress them from the corrections lens, which is
        # a strictly worse bug than the one being fixed — it blinds the lens to the human's
        # primary channel.
        #
        # Idempotent, so an agent that already stamped client-side (Ada does, ada#55) passes
        # through untouched and is not double-marked.
        brief = stamp_dispatched(spec.prompt or f"/{target.slug}:turn", sender=task.agent.slug)
        # Carry the card's title so the runner can NAME the emdash session after the
        # work rather than after its slash command — `c-retry-the-backoff-on-429`
        # instead of `c-turn`, which is what every board dispatch would otherwise
        # read as. `setdefault`: a producer that already chose a name keeps it, and
        # the spec's own provenance keys are untouched (copied, not mutated — the
        # spec is frozen and shared with the task's stored JSON).
        origin_ref = dict(spec.origin_ref)
        origin_ref.setdefault("task_title", task.title)
        turn, _created = services.enqueue_turn(
            agent=target,
            origin=spec.origin,
            idempotency_key=f"task-{task.pk}-{i}",
            prompt=_with_reply(brief, action.comment, action.by),
            origin_ref=origin_ref,
            routing=spec.routing,
            initiator=_dispatch_initiator(task, action),
            # The turn that raised the task (and its conversation) is what this
            # work came FROM — the chain a reader follows back from the new turn.
            parent=_dispatch_parent(task),
        )
        if turn.raised_from_task_id is None:
            turn.raised_from_task = task
            turn.save(update_fields=["raised_from_task"])
        turns.append(turn)
    return turns


def _dispatch_parent(task) -> dict | None:
    """The turn that raised `task`, when one did."""
    raised_by = getattr(task, "raised_by", None) if getattr(task, "raised_by_id", None) else None
    if raised_by is None:
        return None
    parent = {"turn": raised_by}
    if raised_by.chat_session_id:
        parent["session"] = raised_by.chat_session
    return parent


# ---- the board's own turns: approve with no `on_approve`, nudge, reply, answer
#
# A task without `on_approve` used to approve into a pending action row and
# nothing else — no turn, so the agent heard about it only whenever it next
# happened to run (Jonathan, 2026-10-08: approve ALWAYS starts the work). When
# the card carries no spec of its own, canopy-web writes the one turn the
# action implies, from the card itself, and runs it through the same enqueue
# as `dispatch()`: same stamp, same initiator, same parent, same task link.

#: action -> origin_ref["trigger"], what this board turn IS.
TASK_TRIGGERS = {"approve": "task_approve", "nudge": "task_nudge", "reply": "task_reply",
                 "answer": "task_answer"}

#: action -> the label the person's own words are spliced in under.
_TASK_LABELS = {"approve": "APPROVED BY", "nudge": "NUDGED BY", "reply": "NOTE FROM",
                "answer": "ANSWERED BY"}

#: A board turn reuses a turn of the same task that has not STARTED yet rather
#: than stacking a second one behind it — the guard against a double-click, two
#: tabs, or approve-then-nudge. A running turn does not count: a nudge while the
#: agent is mid-turn is a deliberate "and then look again".
_NOT_STARTED = (Turn.QUEUED, Turn.CLAIMED)


def _task_context(task) -> str:
    """What the agent needs to act without re-reading the board first."""
    lines = []
    if task.project_id:
        project = task.project
        lines.append(f"Project: {project.ext_id} · {project.name}")
    for label, value in (("Next action", task.next_action), ("Plan", task.plan),
                         ("Why", task.rationale), ("Source", task.source_url)):
        value = (value or "").strip()
        if value:
            lines.append(f"{label}:\n{value}" if "\n" in value else f"{label}: {value}")
    if task.ask_kind and (task.ask_body or "").strip():
        lines.append(f"The ask on the card:\n{task.ask_body.strip()}")
    return "\n".join(lines)


def task_turn_prompt(task, *, action: str, by: str = "") -> str:
    """The brief for a board turn on `task`, before the stamp and the human's words."""
    who = by or "a person"
    ref = f"{task.agent.slug}/{task.ext_id}"
    if action == "answer":
        head = (f"{who} answered your question on task {task.ext_id}: {task.title}\n\n"
                f"The answer is below. Act on it now — carry on with the work it unblocks, "
                f"or do what it says instead if it redirects you.")
    elif action == "reply":
        head = (f"{who} left a note on task {task.ext_id}: {task.title}\n\n"
                f"Read it, answer it on the task, and fold it into the work if it changes "
                f"anything. Do not restart work the note does not ask for.")
    else:
        verb = ("approved this task on the board" if action == "approve"
                else "nudged this in-progress task on the board")
        head = (f"Work task {task.ext_id}: {task.title}\n\n"
                f"{who} {verb} — work it now, starting from the next action.")
    tail = (f"Report on the task when you stop: update {ref} (status, next_action, notes) "
            f"so the board shows where it stands.")
    context = _task_context(task)
    return "\n\n".join(part for part in (head, context, tail) if part)


def enqueue_task_turn(task, *, action, kind: str | None = None) -> tuple[Turn, bool]:
    """Enqueue the one turn a board action implies for a task with no `on_approve`.

    `kind` overrides the row's action where one action means two things: a
    `reply` that answers an open question is `"answer"` (the agent asked for it,
    so the turn says so and splices it in as ANSWERED BY).

    Returns (turn, created). `action` is the saved AgentTaskAction row: its pk
    keys the turn (`task-<pk>-<action>-<row>`, so every deliberate click is its
    own enqueue and a replayed one is the same turn), and its comment rides
    along as the person's own words.

    An approve or nudge with nothing to say reuses a not-yet-started turn of the
    same task instead of queueing a duplicate — `created` is False then. A reply
    always enqueues: its words are new, and a queued turn's prompt does not
    carry them. So does an answer, for the same reason.
    """
    kind = kind or action.action
    if kind not in TASK_TRIGGERS:
        raise ValueError(f"no board turn for action {kind!r}")
    comment = (action.comment or "").strip()
    if kind not in ("reply", "answer") and not comment:
        waiting = (Turn.objects.filter(raised_from_task=task, status__in=_NOT_STARTED)
                   .order_by("created_at").first())
        if waiting is not None:
            return waiting, False
    brief = stamp_dispatched(task_turn_prompt(task, action=kind, by=action.by),
                             sender=task.agent.slug)
    turn, created = services.enqueue_turn(
        agent=task.agent,
        origin=Turn.ORIGIN_API,
        idempotency_key=f"task-{task.pk}-{kind}-{action.pk}",
        prompt=_with_reply(brief, comment, action.by, label=_TASK_LABELS[kind]),
        origin_ref={"task_title": task.title, "task": task.ext_id,
                    "trigger": TASK_TRIGGERS[kind]},
        initiator=_dispatch_initiator(task, action),
        parent=_dispatch_parent(task),
    )
    if turn.raised_from_task_id is None:
        turn.raised_from_task = task
        turn.save(update_fields=["raised_from_task"])
    return turn, created
