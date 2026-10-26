"""Which agent CLI runs a turn: the vocabulary, what a runner reports it has, and
the one rule that picks (spec 2026-10-26-multi-cli-agents).

A CLI is the program a turn runs in — Claude Code or OpenAI's Codex. It is not a
surface (`Runner.engine`: where a laptop opens a session) and never inferred from
one. An agent allows an ordered list (`Agent.clis`); the claim picks one for each
turn and records it (`Turn.cli`); a conversation keeps the one it started on
(`Session.cli`), because its history lives in that CLI's own store.

Imports nothing from the models at module level: both `agents.models` and
`harness.models` read these constants.
"""
from __future__ import annotations

from dataclasses import dataclass

CLAUDE_CODE = "claude-code"
CODEX = "codex"
CLIS: tuple[str, ...] = (CLAUDE_CODE, CODEX)
CHOICES = [(CLAUDE_CODE, "Claude Code"), (CODEX, "Codex")]
#: What an agent, a repo turn or an agentless chat runs on when nothing says otherwise.
DEFAULT: list[str] = [CLAUDE_CODE]
#: The CLIs a conversation holding a host's sensitive data may run on. Only Claude
#: Code has a zero-data-retention runner today (runner_requirements.SENSITIVE_DATA).
SENSITIVE_CLIS = frozenset({CLAUDE_CODE})


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
