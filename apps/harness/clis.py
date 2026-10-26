"""Which agent CLI runs a turn: the vocabulary, what a runner reports it has, and
the one rule that picks (spec 2026-10-26-multi-cli-agents).

A CLI is the program a turn runs in — Claude Code or OpenAI's Codex. It is not a
surface (`Runner.engine`: where a laptop opens a session) and never inferred from
one. An agent lists the CLIs it is set up for (`Agent.clis`), its default first.
Whoever starts a conversation, a turn or a schedule may choose one of those
(`refusal` says when they may not); otherwise the claim picks from the list. The
claim records the CLI on the turn (`Turn.cli`), and a conversation keeps the one
it started on (`Session.cli`), because its history lives in that CLI's own store.

Imports nothing that reaches the models at module level: both `agents.models` and
`harness.models` read these constants.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import runner_requirements as rr

CLAUDE_CODE = "claude-code"
CODEX = "codex"
CLIS: tuple[str, ...] = (CLAUDE_CODE, CODEX)
CHOICES = [(CLAUDE_CODE, "Claude Code"), (CODEX, "Codex")]
LABELS = dict(CHOICES)
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
    must not become something routing reads. None for anything not a dict."""
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
    that takes a chosen CLI asks this, so a choice the agent cannot run is refused
    when it is made, never left to wait in the queue."""
    if not cli or cli in offered(agent):
        return None
    if agent is None:
        return f"a chat without an agent runs on {label(DEFAULT[0])} only"
    return (f"{agent.name} isn't set up to run on {label(cli)}: add {label(cli)} under "
            "its Settings → Agent CLIs first")


def allowed(turn) -> list[str]:
    """The CLIs this turn may run on, preferred first: its conversation's, once it
    has one (a conversation never changes CLI); else the one it was started on
    (`Turn.cli`, chosen by whoever enqueued it, or a schedule); else what its
    acting agent offers (`offered`)."""
    session = getattr(turn, "chat_session", None) if turn.chat_session_id else None
    if session is not None and session.cli:
        return [session.cli]
    if turn.cli in CLIS:
        return [turn.cli]
    return offered(turn.acting_agent)


def confines(report: CliReport) -> bool:
    """Whether a box can hold a caller's turn to its capability on this CLI: its
    guard and envelope versions for it are the ones `routing.profile_q` requires."""
    from .routing import (  # noqa: PLC0415 — routing imports the models, which import this module
        ENVELOPE_VERSION,
        PROFILES_VERSION,
    )

    return report.profiles >= PROFILES_VERSION and report.envelope >= ENVELOPE_VERSION


def choose(turn, runner, *, reports: dict[str, CliReport] | None = None,
           ignore_signed_in: bool = False) -> str | None:
    """The CLI `runner` runs `turn` on, or None when it may not take it at all.
    THE rule — the claim, coverage and `turn_reach` all ask it, so routing and
    "why is this waiting" cannot disagree. In order: the CLIs the turn allows
    (`allowed`), those the box has signed in, then for a caller's turn only those
    it confines, then for a sensitive conversation only `SENSITIVE_CLIS`; the first
    left wins. `ignore_signed_in` is the counterfactual `turn_reach` asks to tell
    "signed out" from "never". Never bypassed by a pin: it is a security property
    for a confined turn, and a turn on a CLI a box lacks cannot run at all."""
    have = reports if reports is not None else reported(runner)
    sensitive = rr.SENSITIVE_DATA in rr.requirements_of(turn)
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
