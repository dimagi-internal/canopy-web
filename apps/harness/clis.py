"""Which agent CLI runs a turn: the vocabulary, what a runner reports it has, and
the one rule that picks (spec 2026-10-26-multi-cli-agents).

A CLI is the program a turn runs in — Claude Code or OpenAI's Codex. It is not a
surface (`Runner.engine`: where a laptop opens a session) and never inferred from
one. An agent lists the CLIs it is set up for (`Agent.clis`), its default first.
Whoever starts a conversation, a turn or a schedule may choose one of those
(`Turn.requested_cli`, `AgentSchedule.cli`; `refusal` says when they may not);
otherwise the claim picks from the list. The claim records what it picked on the
turn (`Turn.cli`, cleared again if the turn goes back to the queue). A
conversation is held to a CLI (`Session.cli`) once a runner reports a session in
it (`pin_conversation`), because from then on its history lives in that CLI's own
store — or from the start, when its starter chose one.

Adding a CLI is this module (`CLIS`, `CHOICES`) and a label in the frontend; every
schema reads `CliName`.

Imports nothing that reaches the models at module level: both `agents.models` and
`harness.models` read these constants.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from . import runner_requirements as rr

CLAUDE_CODE = "claude-code"
CODEX = "codex"
CLIS: tuple[str, ...] = (CLAUDE_CODE, CODEX)
CHOICES = [(CLAUDE_CODE, "Claude Code"), (CODEX, "Codex")]
LABELS = dict(CHOICES)
#: An agent CLI, as every request and response schema types it: one place, so a
#: new CLI is `CLIS` plus a label, never a hunt for string literals.
CliName = Literal[CLIS]  # type: ignore[valid-type]
#: The same, or "" for "the agent's default" (a schedule's field).
CliOrDefault = Literal[("", *CLIS)]  # type: ignore[valid-type]
#: How a person signs a CLI in on a box, for the advice a waiting turn gives.
SIGN_IN = {CLAUDE_CODE: "`claude /login`", CODEX: "`codex login`"}
#: What an agent, a repo turn or an agentless chat runs on when nothing says otherwise.
DEFAULT: list[str] = [CLAUDE_CODE]
#: The CLIs a conversation holding a host's sensitive data may run on. Only Claude
#: Code has a zero-data-retention runner today (runner_requirements.SENSITIVE_DATA).
SENSITIVE_CLIS = frozenset({CLAUDE_CODE})


def label(cli: str) -> str:
    """A CLI as a person reads it ("Codex"); an unknown value as it is."""
    return LABELS.get(cli, cli)


def default_list() -> list[str]:
    """A fresh `DEFAULT` for a JSONField default (never the shared list itself)."""
    return list(DEFAULT)


# ---- what a runner reports --------------------------------------------------
@dataclass(frozen=True)
class CliReport:
    """One CLI on a box, as the box last reported it: signed in, and which guard
    (`profiles`) and caller-envelope (`envelope`) versions it enforces for it —
    the same two numbers `routing.profile_q` reads, per CLI."""

    signed_in: bool
    profiles: int = 0
    envelope: int = 0
    version: str = ""


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def sanitize(raw) -> dict[str, dict] | None:
    """A heartbeat's `clis` as it may be stored: only known CLIs, each value
    coerced to its four fields. Never trusted from the wire as sent — like the
    other capabilities it is a hint the box makes about itself, and a junk key
    must not become something routing reads. None for anything not a dict.
    (`unknown_names` keeps the names this server does not know, for display.)"""
    if not isinstance(raw, dict):
        return None
    out: dict[str, dict] = {}
    for cli, value in raw.items():
        if cli not in CLIS or not isinstance(value, dict):
            continue
        out[cli] = {
            "signed_in": value.get("signed_in") is True,
            "profiles": _int(value.get("profiles")),
            "envelope": _int(value.get("envelope")),
            "version": str(value.get("version") or "")[:32],
        }
    return out


def unknown_names(raw) -> list[str]:
    """The CLIs a heartbeat names that this server does not know (a newer runner
    on an older server): never routed to, but shown, so a box that takes no turns
    because it reports only those says why rather than looking broken."""
    if not isinstance(raw, dict):
        return []
    return sorted(str(c)[:32] for c in raw if c not in CLIS)[:8]


def reported(runner) -> dict[str, CliReport]:
    """The CLIs this box has, from its last heartbeat. A box that reports none is
    read as every box was before CLIs existed: Claude Code alone, signed in unless
    it said otherwise (`Runner.signed_out`), confining at the box-wide
    `profiles`/`envelope` it reports. So nothing changes for a box until its own
    code says it has more."""
    caps = runner.capabilities or {}
    raw = caps.get("clis")
    if isinstance(raw, dict):
        return {cli: CliReport(v["signed_in"], v["profiles"], v["envelope"], v["version"])
                for cli, v in (sanitize(raw) or {}).items()}
    return {CLAUDE_CODE: CliReport(not runner.signed_out, _int(caps.get("profiles")),
                                   _int(caps.get("envelope")))}


# ---- the one rule -----------------------------------------------------------
def offered(agent) -> list[str]:
    """The CLIs `agent` is set up to run on, its default first (`Agent.clis`);
    `DEFAULT` for no agent (a repo turn, an agentless chat), or for an agent whose
    list names nothing known."""
    if agent is not None:
        listed = [c for c in dict.fromkeys(agent.clis or []) if c in CLIS]
        if listed:
            return listed
    return list(DEFAULT)


def refusal(agent, cli: str) -> str | None:
    """Why a conversation, turn or schedule may not choose `cli` for `agent`, or
    None when it may (blank = no choice: the agent's default decides). Every door
    that takes a chosen CLI asks this, so a choice nothing can run is refused when
    it is made, never left to wait in the queue: the agent must be set up for it,
    and some runner serving its workspace must have it (`available`)."""
    if not cli:
        return None
    if cli not in offered(agent):
        if agent is None:
            return f"a chat without an agent runs on {label(DEFAULT[0])} only"
        return (f"{agent.name} isn't set up to run on {label(cli)}: add {label(cli)} under "
                "its Settings → Agent CLIs first")
    if agent is not None and cli not in available(agent.workspace_id):
        return not_available(cli)
    return None


def not_available(cli: str) -> str:
    return (f"no runner serving this workspace has {label(cli)} yet: set {label(cli)} up on "
            "a runner first")


def available(workspace_slug: str) -> set[str]:
    """The CLIs some runner serving `workspace_slug` reports having — signed in or
    not, online or not: a box that HAS a CLI is one a person can sign in, while a
    CLI no box has is a choice nothing can ever run. Retired boxes do not count."""
    from .models import Runner  # noqa: PLC0415 — the models import this module
    from .routing import runner_tenant_slugs_of  # noqa: PLC0415 — likewise

    runners = list(Runner.objects.exclude(status=Runner.RETIRED).filter(owner__isnull=False)
                   .prefetch_related("enrollments"))
    tenants = runner_tenant_slugs_of(runners)
    out: set[str] = set()
    for r in runners:
        if workspace_slug in tenants[r.pk]:
            out.update(reported(r))
    return out


def list_refusal(agent, new: list[str]) -> str | None:
    """Why `agent` may not be set up for exactly the CLIs `new`, or None. A CLI
    added must be one some runner serving its workspace has (`available`) — else
    every chat, schedule or flag that picks it would wait forever. A CLI removed
    must not be one a schedule still names: say which, rather than leave them
    firing turns nothing may run."""
    from .models import AgentSchedule  # noqa: PLC0415 — the models import this module

    added = [c for c in new if c not in offered(agent)]
    if added:
        have = available(agent.workspace_id)
        if missing := [c for c in added if c not in have]:
            return not_available(missing[0])
    removed = [c for c in offered(agent) if c not in new]
    if removed:
        names = list(AgentSchedule.objects.filter(agent=agent, cli__in=removed)
                     .values_list("name", flat=True)[:5])
        if names:
            return (f"schedule{'s' if len(names) > 1 else ''} {', '.join(names)} "
                    f"run{'' if len(names) > 1 else 's'} on {_or(removed)}: change "
                    f"{'them' if len(names) > 1 else 'it'} first")
    return None


def allowed(turn) -> list[str]:
    """The CLIs this turn may run on, preferred first: its conversation's, once it
    is held to one (a conversation never changes CLI); else the one its starter or
    schedule asked for (`Turn.requested_cli`); else what its acting agent offers
    (`offered`). Never a CLI the agent is no longer set up for: an admin who takes
    Codex off an agent stops its Codex work, conversations included (empty list —
    `blocker` says so)."""
    session = getattr(turn, "chat_session", None) if turn.chat_session_id else None
    if session is not None and session.cli:
        wanted = [session.cli]
    elif turn.requested_cli in CLIS:
        wanted = [turn.requested_cli]
    else:
        return offered(turn.acting_agent)
    have = offered(turn.acting_agent)
    return [c for c in wanted if c in have]


def conversation_refusal(session) -> str | None:
    """Why nothing more can be sent into `session` on the CLI it is held to: its
    agent is no longer set up for it. None when it can (or holds to none yet)."""
    if session is None or not session.cli or session.cli in offered(session.agent):
        return None
    return (f"this conversation runs on {label(session.cli)}, and "
            f"{session.agent.name if session.agent else 'its agent'} is no longer set up for "
            f"{label(session.cli)}: start a new conversation")


def pin_conversation(session_ids, cli: str) -> None:
    """Hold these conversations to `cli`, where none is held yet: a runner just
    reported a session in it, so its history now lives in that CLI's store. First
    write wins, so a later report never moves it."""
    from apps.canopy_sessions.models import Session  # noqa: PLC0415 — models import this module

    ids = [pk for pk in session_ids if pk]
    if ids and cli in CLIS:
        Session.objects.filter(pk__in=ids, cli="").update(cli=cli)


def confines(report: CliReport) -> bool:
    """Whether a box can hold a caller's turn to its capability on this CLI: its
    guard and envelope versions for it are the ones `routing.profile_q` requires."""
    from .routing import (  # noqa: PLC0415 — routing imports the models, which import this module
        ENVELOPE_VERSION,
        PROFILES_VERSION,
    )

    return report.profiles >= PROFILES_VERSION and report.envelope >= ENVELOPE_VERSION


def choose(turn, runner, *, reports: dict[str, CliReport] | None = None,
           ignore_signed_in: bool = False, ignore_requirements: bool = False) -> str | None:
    """The CLI `runner` runs `turn` on, or None when it may not take it at all.
    THE rule — the claim, coverage and `turn_reach` all ask it, so routing and
    "why is this waiting" cannot disagree. In order: the CLIs the turn allows
    (`allowed`), those the box has signed in, then for a caller's turn only those
    it confines, then for a sensitive conversation only `SENSITIVE_CLIS`; the first
    left wins. `ignore_signed_in` is the counterfactual `turn_reach` asks to tell
    "signed out" from "never", and `ignore_requirements` the one coverage asks to
    tell a runner-requirement block from any other. Never bypassed by a pin: it is
    a security property for a confined turn, and a turn on a CLI a box lacks cannot
    run at all."""
    have = reports if reports is not None else reported(runner)
    sensitive = not ignore_requirements and rr.SENSITIVE_DATA in rr.requirements_of(turn)
    for cli in allowed(turn):
        report = have.get(cli)
        if report is None or not (report.signed_in or ignore_signed_in):
            continue
        if turn.capability and not confines(report):
            continue
        if sensitive and cli not in SENSITIVE_CLIS:
            continue
        return cli
    return None


# ---- why a turn waits on its CLI --------------------------------------------
def blocker(turn, runners) -> str:
    """Why none of `runners` (the ones that would otherwise take `turn`) can run
    it on a CLI, as advice a person can act on — the most specific cause first:
    the agent no longer runs the conversation's CLI; a sensitive conversation on
    an agent set up for no CLI that may hold it; a box that has the CLI signed in
    but cannot confine a caller's turn on it; or no box has it at all."""
    agent = turn.acting_agent
    who = agent.name if agent is not None else "this turn"
    wants = allowed(turn)
    if not wants:
        held = (turn.chat_session.cli if turn.chat_session_id and turn.chat_session.cli
                else turn.requested_cli)
        return (f"it runs on {label(held)}, and {who} is no longer set up for "
                f"{label(held)} — start a new conversation, or add {label(held)} back")
    if rr.SENSITIVE_DATA in rr.requirements_of(turn) and not set(wants) & SENSITIVE_CLIS:
        ok = " or ".join(label(c) for c in sorted(SENSITIVE_CLIS))
        return (f"it holds sensitive data, which runs on {ok} only, and it may run only on "
                f"{_or(wants)}")
    reports = [reported(r) for r in runners]
    if turn.capability:
        has = [c for c in wants if any(c in rep for rep in reports)]
        if has:
            return (f"it is a caller's turn, and no runner that would take it can confine one on "
                    f"{_or(has)} yet — update the runner so it enforces the current guards")
    signs = " or ".join(f"{label(c)} ({SIGN_IN.get(c, 'its login')})" for c in wants)
    return f"it runs on {_or(wants)}, and none of those runners has it set up — sign in {signs} on one"


def _or(clis_: list[str]) -> str:
    return " or ".join(label(c) for c in clis_)
